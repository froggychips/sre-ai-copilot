import asyncio
import logging
import weakref
from typing import Any, Dict, Optional

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.claude_cli_service import ClaudeCliService
from app.services.resilience import LLMCircuitOpen, llm_retry_strategy

_LLM_PROVIDER = "anthropic"

# Отдельный именованный logger для circuit-диагностики: не смешиваем с
# root-`logging.warning` (его патчат тесты вокруг truncation-warning-а), и
# в агрегаторе логов деградацию брейкера видно по своему префиксу.
_circuit_log = logging.getLogger(__name__ + ".circuit")


class LLMTruncatedResponse(Exception):
    """Ответ LLM обрезан по max_tokens, а вызывающему нужен ПОЛНЫЙ ответ.

    Поднимается НЕ здесь, а в BaseAgent.ask для JSON-агентов
    (json_response=True: multi_hypothesis / fact_critic). Обрезанный JSON
    их парсеры молча превращали в пустой список — неотличимо от честного
    «гипотез/опровержений нет»: слабая гипотеза «переживала» критику с
    полной confidence и уверенно-неверный вывод уходил людям (кодревью
    2026-08; generate_full вычислял truncated=True, но флаг терялся).

    Ретраи — СОЗНАТЕЛЬНО нигде:
      * уровень LLM-вызова: исключение живёт ВЫШЕ llm_retry_strategy
        (generate_full по-прежнему ВОЗВРАЩАЕТ truncated-флаг, а не raise),
        поэтому retry-слой его в принципе не видит; повтор того же промпта
        с тем же MAX_TOKENS обрежется детерминированно снова;
      * уровень Celery: наследуемся от Exception (НЕ от OSError/
        ConnectionError), т.е. не входим в RETRIABLE_EXC (app/workers/
        tasks.py) — async_process_incident фиксирует терминальный фейл
        стадии (post-mortem + FAILED) без пережигания LLM-бюджета.
    """


def _get_resilience():
    """Лениво достаём LLMResilienceManager-синглтон из celery_worker.

    Ленивый импорт рвёт цикл llm_service↔celery_worker.
    Ошибка ИМПОРТА → None: circuit breaker — best-effort и НИКОГДА не должен
    ронять сам LLM-вызов; но деградацию логируем — молчаливый no-op брейкера
    уже приводил к тому, что fan-out storm шёл в лежащий провайдер.
    """
    try:
        from app.celery_worker import resilience
        return resilience
    except Exception as e:
        _circuit_log.warning("llm_resilience_unavailable: %s", e)
        return None


async def _report_provider(resilience, *, success: bool) -> None:
    """report_success/report_failure; ошибки резильенса не роняют вызов.

    Не роняют — но и не глотаются молча: если report_* стабильно падает
    (Redis умер, клиент от чужого loop-а), брейкер слеп и это надо видеть
    в логах, а не постфактум по счетам за токены.
    """
    if resilience is None:
        return
    try:
        if success:
            await resilience.report_success(_LLM_PROVIDER)
        else:
            await resilience.report_failure(_LLM_PROVIDER)
    except Exception as e:
        _circuit_log.warning(
            "llm_circuit_report_failed (success=%s): %s: %s",
            success, type(e).__name__, e,
        )


class LLMService:
    """LLM-фасад с двумя backend-ами:

    - `anthropic` (default, production) — AsyncAnthropic SDK с ANTHROPIC_API_KEY.
    - `claude_cli` (local PoC / e2e без ключа) — subprocess вокруг
      Claude Code CLI в `--print` режиме, использует CLI-авторизацию.

    Контракт:
      generate_content(prompt) -> str
        Legacy/simple — возвращает только text. Backward-compat для прямых
        callers.
      generate_full(prompt) -> dict
        Возвращает {text, input_tokens, output_tokens, model, backend}.
        Используется BaseAgent.ask() для записи реальных token-usage в
        Prometheus per agent. Для claude_cli backend usage=None
        (subprocess не возвращает usage info).
    """

    def __init__(self) -> None:
        self.backend = (settings.LLM_BACKEND or "anthropic").lower()
        self.model = settings.MODEL_NAME
        self.cli: Optional[ClaudeCliService] = None
        # `client` — ЯВНЫЙ override (тесты ставят svc.client = MagicMock()).
        # В проде остаётся None: реальный AsyncAnthropic берётся per-event-loop
        # из _loop_clients (см. _anthropic_client). Модульный singleton
        # llm_client живёт через МНОГО `asyncio.run(...)` Celery-задач
        # (worker_max_tasks_per_child=50), а AsyncAnthropic держит httpx-pool
        # и asyncio-локи, привязанные к loop-у создания — общий клиент давал
        # "Event loop is closed"/"attached to a different loop" со второй
        # задачи в каждом child-процессе.
        self.client: Optional[AsyncAnthropic] = None
        self._loop_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncAnthropic]" = (
            weakref.WeakKeyDictionary()
        )
        if self.backend == "claude_cli":
            self.cli = ClaudeCliService(
                model=settings.MODEL_NAME or None,
                timeout_seconds=float(
                    getattr(settings, "CLAUDE_CLI_TIMEOUT_SECONDS", 180.0)
                ),
            )

    def _build_anthropic_client(self) -> AsyncAnthropic:
        # max_retries=0 — НЕ полагаемся на встроенные ретраи SDK (по дефолту 2).
        # Иначе SDK-ретраи (2) × наш llm_retry_strategy (3) = до 9 HTTP на один
        # agent-вызов (retry-storm + сжигание токенов). Единственный слой
        # ретраев — наш декоратор; предикат там сужен до транзиентных ошибок.
        # timeout на уровне клиента — реальная отмена httpx-запроса по дедлайну
        # (внешний asyncio.wait_for НЕ обрывает уже улетевший HTTP → «зомби»-
        # запрос жжёт токены в фоне). LLM_TIMEOUT_SECONDS из settings.
        return AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            max_retries=0,
            timeout=float(getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)),
        )

    def _anthropic_client(self) -> AsyncAnthropic:
        """AsyncAnthropic, привязанный к ТЕКУЩЕМУ event loop.

        Явный override self.client (тесты / кастомный клиент) имеет приоритет.
        Иначе — клиент из per-loop кэша: каждый `asyncio.run` получает свой
        httpx-pool, клиенты умерших loop-ов уходят вместе с loop-ом
        (WeakKeyDictionary → GC).
        """
        if self.client is not None:
            return self.client
        loop = asyncio.get_running_loop()
        client = self._loop_clients.get(loop)
        if client is None:
            client = self._build_anthropic_client()
            self._loop_clients[loop] = client
        return client

    @llm_retry_strategy
    async def generate_full(self, prompt: str) -> Dict[str, Any]:
        """Single LLM round-trip с возвратом usage-info.

        Возвращает:
          {text: str, input_tokens: int, output_tokens: int,
           model: str, backend: str,
           stop_reason: Optional[str], truncated: bool}
        Для claude_cli (subprocess без usage API) input/output_tokens = 0
        (правильнее чем char-approximation — counter с 0 не искажает sum);
        stop_reason=None, truncated=False (subprocess не отдаёт stop_reason).
        truncated=True при stop_reason="max_tokens" (anthropic) — ответ оборван
        по лимиту, JSON может быть невалиден; см. логику ниже.
        """
        # Circuit breaker применяем только к anthropic-провайдеру; claude_cli —
        # локальный subprocess, у него своя модель отказа (resilience=None →
        # _report_provider/проверка circuit no-op).
        resilience = None if self.backend == "claude_cli" else _get_resilience()
        try:
            if self.backend == "claude_cli":
                assert self.cli is not None
                text = await self.cli.generate_content(prompt)
                return {
                    "text": text,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "model": self.model,
                    "backend": "claude_cli",
                    # claude_cli (subprocess --print) не отдаёт stop_reason →
                    # обрезку по max_tokens здесь не отличить; считаем не-обрезанным.
                    "stop_reason": None,
                    "truncated": False,
                }
            client = self._anthropic_client()
            # Circuit breaker (resilience.py): fail fast, если anthropic уже
            # сыпет ошибками — не долбим лежащий провайдер. Fail-open: ошибка
            # самого резильенса/Redis → считаем circuit закрытым и идём дальше
            # (иначе сбой Redis глушил бы ВСЕ LLM-вызовы), но деградацию
            # логируем громко — молчаливое `except: circuit_open = False`
            # уже превращало брейкер в no-op незаметно для всех.
            if resilience is not None:
                try:
                    circuit_open = await resilience.is_circuit_open(_LLM_PROVIDER)
                except Exception as e:
                    _circuit_log.warning(
                        "llm_circuit_check_failed (fail-open): %s: %s",
                        type(e).__name__, e,
                    )
                    circuit_open = False
                if circuit_open:
                    raise LLMCircuitOpen(
                        f"LLM circuit open for provider {_LLM_PROVIDER!r}"
                    )
            llm_timeout = getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)
            # NB: НЕ добавлять Anthropic prompt caching (cache_control) сюда.
            # Разбор 2026-06: в IncidentPipeline каждый агент шлёт уникальный
            # контент (потребляет вывод предыдущей стадии), а стабильный префикс
            # (role+instruction из BaseAgent) — пара сотен токенов, ниже
            # 4096-токенного минимума кэша Opus → cache_control молча не кэширует
            # (cache_creation_input_tokens=0) и на уникальных префиксах ещё и
            # добавляет 1.25× write-премию без reads = НЕТТО-МИНУС по стоимости.
            # NB: response-кэша в боевом пути НЕТ — BaseAgent.ask зовёт LLM
            # напрямую (каждый вызов = реальный round-trip; идентичные ретраи
            # не дедуплицируются). Единственная
            # точка, где prompt caching был бы net-positive — общий префикс
            # fan-out'а MultiHypothesisAgent (shared-prefix/varying-suffix); это
            # отдельный hot-path рефактор, не «воткнуть cache_control».
            # Двухслойный таймаут:
            #  1) per-request timeout= на messages.create → httpx обрывает сам
            #     HTTP-запрос по дедлайну (реальная отмена, не «зомби»; SDK
            #     поднимет APITimeoutError). Дублирует client-level timeout, но
            #     явный per-request делает дедлайн читаемым на call-site.
            #  2) внешний asyncio.wait_for как верхняя граница — страховка на
            #     случай, если корутина зависнет ВНЕ httpx (DNS/телo/локи).
            #     Он НЕ отменяет сетевой сокет сам по себе — поэтому слой (1)
            #     обязателен, а wait_for оставлен лишь как hard-ceiling.
            response = await asyncio.wait_for(
                client.messages.create(
                    model=self.model,
                    max_tokens=settings.MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=llm_timeout,
                ),
                timeout=llm_timeout,
            )
            # Anthropic ContentBlock = TextBlock | ToolUseBlock; .text только у TextBlock.
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            if not text:
                raise ValueError("Empty response from LLM")
            # stop_reason="max_tokens" → ответ оборван по лимиту, а не закончен
            # естественно. Для JSON-агентов (multi_hypothesis/fact_critic/fix)
            # обрезка даёт невалидный JSON, который парсеры молча превращают в
            # пусто — неотличимо от честного пустого ответа. Делаем обрезку
            # ВИДИМОЙ: warning + признак truncated в dict (НЕ роняем вызов —
            # частичный ответ всё равно может быть полезен вызывающему).
            # Довод до фейла — этажом выше: BaseAgent.ask для агентов с
            # json_response=True поднимает LLMTruncatedResponse (raise здесь
            # нельзя: он попал бы под llm_retry_strategy и сломал прозаические
            # вызовы, которым огрызок текста полезен).
            stop_reason = getattr(response, "stop_reason", None)
            truncated = stop_reason == "max_tokens"
            if truncated:
                logging.warning(
                    "LLM response truncated by max_tokens "
                    "(model=%s, max_tokens=%s, text_len=%d)",
                    self.model,
                    settings.MAX_TOKENS,
                    len(text),
                )
            # Anthropic SDK: response.usage.input_tokens / .output_tokens
            usage = getattr(response, "usage", None)
            await _report_provider(resilience, success=True)
            return {
                "text": text,
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "model": self.model,
                "backend": "anthropic",
                "stop_reason": stop_reason,
                "truncated": truncated,
            }
        except LLMCircuitOpen:
            # Брейкер сработал — это НЕ новый сбой провайдера, не считаем его
            # и не ретраим (см. llm_retry_strategy).
            raise
        except asyncio.TimeoutError as e:
            await _report_provider(resilience, success=False)
            logging.error("LLM call timed out")
            # `from e` сохраняет __cause__=TimeoutError → is_retryable_llm_error
            # распознаёт ретраибельность сквозь обёртку ValueError (таймаут =
            # транзиент, повтор оправдан).
            raise ValueError("LLM timeout") from e
        except Exception as e:
            await _report_provider(resilience, success=False)
            logging.error(f"LLM call attempt failed: {e}")
            raise

    async def generate_content(self, prompt: str) -> str:
        """Backward-compat: возвращает только text. Внутри идёт через generate_full."""
        return (await self.generate_full(prompt))["text"]


llm_client = LLMService()
