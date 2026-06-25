import asyncio
import logging
from typing import Any, Dict, Optional

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.claude_cli_service import ClaudeCliService
from app.services.resilience import LLMCircuitOpen, llm_retry_strategy

_LLM_PROVIDER = "anthropic"


def _get_resilience():
    """Лениво достаём LLMResilienceManager-синглтон из celery_worker.

    Ленивый импорт (как в llm_cache.py) рвёт цикл llm_service↔celery_worker.
    Любая ошибка → None: circuit breaker — best-effort и НИКОГДА не должен
    ронять сам LLM-вызов.
    """
    try:
        from app.celery_worker import resilience
        return resilience
    except Exception:
        return None


async def _report_provider(resilience, *, success: bool) -> None:
    """report_success/report_failure, проглатывая любые ошибки резильенса."""
    if resilience is None:
        return
    try:
        if success:
            await resilience.report_success(_LLM_PROVIDER)
        else:
            await resilience.report_failure(_LLM_PROVIDER)
    except Exception:
        pass


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
        self.client: Optional[AsyncAnthropic] = None
        if self.backend == "claude_cli":
            self.cli = ClaudeCliService(
                model=settings.MODEL_NAME or None,
                timeout_seconds=float(
                    getattr(settings, "CLAUDE_CLI_TIMEOUT_SECONDS", 180.0)
                ),
            )
        else:
            # max_retries=0 — НЕ полагаемся на встроенные ретраи SDK (по дефолту 2).
            # Иначе SDK-ретраи (2) × наш llm_retry_strategy (3) = до 9 HTTP на один
            # agent-вызов (retry-storm + сжигание токенов). Единственный слой
            # ретраев — наш декоратор; предикат там сужен до транзиентных ошибок.
            # timeout на уровне клиента — реальная отмена httpx-запроса по дедлайну
            # (внешний asyncio.wait_for НЕ обрывает уже улетевший HTTP → «зомби»-
            # запрос жжёт токены в фоне). LLM_TIMEOUT_SECONDS из settings.
            self.client = AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                max_retries=0,
                timeout=float(getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)),
            )

    @llm_retry_strategy
    async def generate_full(self, prompt: str) -> Dict[str, Any]:
        """Single LLM round-trip с возвратом usage-info.

        Возвращает:
          {text: str, input_tokens: int, output_tokens: int,
           model: str, backend: str}
        Для claude_cli (subprocess без usage API) input/output_tokens = 0
        (правильнее чем char-approximation — counter с 0 не искажает sum).
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
                }
            assert self.client is not None
            # Circuit breaker (resilience.py): fail fast, если anthropic уже
            # сыпет ошибками — не долбим лежащий провайдер. Fail-open: любая
            # ошибка резильенса/redis → считаем circuit закрытым, идём дальше.
            if resilience is not None:
                try:
                    circuit_open = await resilience.is_circuit_open(_LLM_PROVIDER)
                except Exception:
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
            # Реальный повтор (идентичные ретраи) уже покрыт llm_cache.py
            # (Redis response-cache по role+instruction+context). Единственная
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
                self.client.messages.create(
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
            # Anthropic SDK: response.usage.input_tokens / .output_tokens
            usage = getattr(response, "usage", None)
            await _report_provider(resilience, success=True)
            return {
                "text": text,
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "model": self.model,
                "backend": "anthropic",
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
