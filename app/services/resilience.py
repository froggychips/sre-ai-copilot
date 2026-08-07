import asyncio
import inspect
import random
import time
import weakref
from functools import wraps
from typing import Any, Callable, Optional, Tuple, Type

import anthropic
import httpx
import structlog
from redis.asyncio import Redis, from_url

logger = structlog.get_logger()


class LoopLocalRedis:
    """Прокси над redis.asyncio-клиентом с кэшем «клиент per event loop».

    Проблема: Celery-таски гоняют каждую задачу через собственный
    `asyncio.run(...)` (новый loop), а worker-процесс живёт десятки задач
    (`worker_max_tasks_per_child=50`). Модульный `from_url(...)` создаёт ОДИН
    connection pool, чьи коннекты привязываются к loop-у первой задачи — все
    последующие задачи получают `RuntimeError: Event loop is closed` /
    "attached to a different loop", и best-effort-потребители (circuit
    breaker) молча деградируют в no-op.

    Прокси резолвит реальный клиент лениво, при обращении к атрибуту:
      * есть running loop → клиент из WeakKeyDictionary по loop-у
        (свой пул на каждый `asyncio.run`);
      * нет running loop (sync-контекст) → общий fallback-клиент
        (прежнее поведение).

    Клиенты умерших loop-ов удаляются из кэша сборщиком мусора вместе с
    loop-ом (WeakKeyDictionary); их сокеты закрывает GC. Для процесса с
    max_tasks_per_child=50 это ограниченный и приемлемый хвост.
    """

    def __init__(self, url: str):
        # Прямая установка через __dict__ не нужна: __getattr__ вызывается
        # только для ОТСУТСТВУЮЩИХ атрибутов, _url/_clients/_fallback найдутся.
        self._url = url
        self._clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Redis]" = (
            weakref.WeakKeyDictionary()
        )
        self._fallback: Optional[Redis] = None

    def _resolve(self) -> Redis:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            if self._fallback is None:
                self._fallback = from_url(self._url)
            return self._fallback
        client = self._clients.get(loop)
        if client is None:
            client = from_url(self._url)
            self._clients[loop] = client
        return client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


class LLMCircuitOpen(Exception):
    """Брейкер провайдера открыт — fail fast, без ретраев.

    Отличается от транзиентного сбоя вызова: открытый circuit означает, что
    мы СОЗНАТЕЛЬНО не зовём провайдера, поэтому llm_retry_strategy не должен
    его ретраить (иначе просто 3× перепроверит открытый circuit с backoff-ом).
    """


def is_retryable_llm_error(exc: BaseException) -> bool:
    """Предикат: ретраибельна ли ошибка LLM-вызова.

    Ретраим ТОЛЬКО транзиентные сбои, где повтор имеет смысл:
      - APIConnectionError  — сеть упала до ответа (вкл. APITimeoutError,
        который её сабкласс);
      - RateLimitError (429) — провайдер перегружен, backoff поможет;
      - APIStatusError с 5xx — серверная ошибка/overloaded (500/529).

    НЕ ретраим детерминированные клиентские ошибки — повтор того же запроса
    даст тот же результат и просто сожжёт квоту/токены:
      400 BadRequest / 401 Authentication / 403 PermissionDenied /
      404 NotFound / 422 UnprocessableEntity.

    LLMCircuitOpen намеренно НЕ ретраибелен — брейкер открыт сознательно
    (см. llm_retry_strategy, где он ещё и пробрасывается до предиката).

    Внешний asyncio.wait_for в llm_service конвертирует таймаут в
    asyncio.TimeoutError — его тоже считаем ретраибельным (транзиент).
    """
    if isinstance(exc, LLMCircuitOpen):
        return False
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        # APITimeoutError — подкласс APIConnectionError, покрыт здесь же.
        return True
    if isinstance(exc, anthropic.APIStatusError):
        # 5xx (500 api_error / 529 overloaded) — транзиент; 4xx — нет.
        return exc.status_code >= 500
    if isinstance(exc, asyncio.TimeoutError):
        # Верхняя граница wait_for сработала — повтор оправдан.
        return True
    # llm_service оборачивает таймаут в ValueError("LLM timeout") from e —
    # разворачиваем __cause__, чтобы не потерять ретраибельность.
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_retryable_llm_error(cause)
    return False


# Кап на Retry-After от провайдера: злой/битый заголовок не должен
# подвешивать worker на минуты.
_RETRY_AFTER_CAP_SECONDS = 30.0


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Retry-After из 429-ответа провайдера (секунды), если он есть.

    Anthropic SDK кладёт httpx.Response в exc.response — читаем заголовок
    оттуда. None при отсутствии/непарсибельности (fallback на наш backoff).
    Значение зажимается капом _RETRY_AFTER_CAP_SECONDS.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, _RETRY_AFTER_CAP_SECONDS)


def llm_retry_strategy(func):
    """Async retry decorator для ТРАНЗИЕНТНЫХ сбоев LLM-вызова.

    Ретраит по предикату is_retryable_llm_error (5xx / rate-limit / сеть /
    таймаут). Неретраибельные 4xx (400/401/403/404/422) и LLMCircuitOpen
    пробрасываются сразу — без повторов и backoff-а.

    Backoff: линейная база × jitter (±50%). Без jitter-а параллельные
    perspective-агенты MultiHypothesisAgent, получив 429 одновременно,
    ретраились синхронными волнами — и снова упирались в rate-limit все
    разом. На 429 дополнительно уважаем Retry-After провайдера (с капом),
    поверх него — небольшой случайный разброс, чтобы fan-out не проснулся
    одним фронтом.

    NB: SDK-клиент сконфигурён с max_retries=0 (см. llm_service), поэтому
    единственный слой ретраев — этот декоратор; двойного умножения попыток
    (SDK × декоратор) больше нет.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        retries = 3
        base_delay = 0.5
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return await func(*args, **kwargs)
            except LLMCircuitOpen:
                raise  # брейкер открыт → fail fast, не ретраим
            except Exception as exc:
                if not is_retryable_llm_error(exc):
                    # Детерминированная клиентская ошибка (4xx и т.п.) —
                    # повтор бессмыслен, пробрасываем немедленно.
                    raise
                last_exc = exc
                logger.warning("llm_call_retry", attempt=attempt, error=str(exc))
                if attempt < retries:
                    retry_after = None
                    if isinstance(exc, anthropic.RateLimitError):
                        retry_after = _retry_after_seconds(exc)
                    if retry_after is not None:
                        # Провайдер сказал сколько ждать — ждём его срок,
                        # плюс разброс до 0.5s чтобы разлепить волну.
                        delay = retry_after + random.uniform(0, 0.5)
                    else:
                        # Линейный backoff с jitter ±50%.
                        delay = base_delay * attempt * random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay)
        raise last_exc

    return wrapper


# Дефолтный retry_on для with_external_retry — только ТРАНЗИЕНТНЫЕ классы:
#   * OSError — покрывает ConnectionError/TimeoutError/socket-ошибки
#     (все они его сабклассы);
#   * httpx.TransportError — сетевой слой httpx (ConnectError, ReadTimeout,
#     PoolTimeout, RemoteProtocolError и т.д.).
# НЕ входит httpx.HTTPStatusError: 4xx/SQL-ошибки детерминированы, их повтор
# лишь жжёт время и квоту (прежний дефолт `(Exception,)` ретраил 401/400 3×).
_DEFAULT_TRANSIENT_ERRORS: Tuple[Type[BaseException], ...] = (
    OSError,
    httpx.TransportError,
)


def with_external_retry(
    *,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    retry_on: Tuple[Type[BaseException], ...] = _DEFAULT_TRANSIENT_ERRORS,
    name: str = "",
) -> Callable[[Callable], Callable]:
    """Универсальный retry-декоратор для enrichment-integrations.

    Применяется к Jira/TeamCity/VictoriaMetrics/ClickHouse/Statics клиентам —
    transient network/timeout ошибки ретрайнятся с exponential backoff,
    финальное исключение пробрасывается (graceful-degrade на стороне caller-а
    уже есть — `try/except`-фолбэк превращает `None` в "feature недоступна").

    Поддерживает и async и sync функции (определяется через
    inspect.iscoroutinefunction). Sync-вариант используется в
    `app/services/clickhouse_service.py` / `app/services/statics_service.py`
    которые вызываются через `asyncio.to_thread`.

    Параметры:
      max_attempts: общее число попыток (1 = no retry).
      initial_delay: первая пауза в секундах.
      backoff_factor: множитель delay для каждой следующей попытки
        (2.0 = exponential: 0.5s, 1.0s, 2.0s).
      retry_on: tuple типов исключений, на которых retry-имся.
        По дефолту только транзиентные сетевые классы
        (_DEFAULT_TRANSIENT_ERRORS: OSError + httpx.TransportError).
        Детерминированные ошибки (HTTP 4xx, битый SQL) по дефолту НЕ
        ретраятся — их повтор даёт тот же результат. Для клиентов с
        другой моделью отказа передавайте свой tuple (см.
        statics_service: psycopg2.OperationalError/InterfaceError).
      name: имя для structlog (по дефолту имя функции).

    Когда НЕ применять:
      - Long-running операции (read-once dump, full-resync) — retry
        масштабирует cost.
      - Idempotent-uncertain writes (POST без idempotency-key).
    """
    def decorator(func: Callable) -> Callable:
        retry_name = name or func.__name__

        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                delay = initial_delay
                last_exc: BaseException | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except retry_on as exc:
                        last_exc = exc
                        if attempt < max_attempts:
                            logger.warning(
                                "external_retry",
                                fn=retry_name,
                                attempt=attempt,
                                max_attempts=max_attempts,
                                next_delay_s=delay,
                                error_type=type(exc).__name__,
                                error=str(exc)[:200],
                            )
                            await asyncio.sleep(delay)
                            delay *= backoff_factor
                        else:
                            logger.error(
                                "external_retry_exhausted",
                                fn=retry_name,
                                attempts=max_attempts,
                                error_type=type(exc).__name__,
                                error=str(exc)[:200],
                            )
                assert last_exc is not None
                raise last_exc

            return async_wrapper

        # sync branch
        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        logger.warning(
                            "external_retry",
                            fn=retry_name,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            next_delay_s=delay,
                            error_type=type(exc).__name__,
                            error=str(exc)[:200],
                        )
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        logger.error(
                            "external_retry_exhausted",
                            fn=retry_name,
                            attempts=max_attempts,
                            error_type=type(exc).__name__,
                            error=str(exc)[:200],
                        )
            assert last_exc is not None
            raise last_exc

        return sync_wrapper

    return decorator


# ── Circuit breaker параметры ────────────────────────────────────────────────
# fail-окно якорится на ПЕРВОМ сбое: 5 сбоев за 60s → open на 60s.
_CB_FAIL_WINDOW_SECONDS = 60
_CB_FAIL_THRESHOLD = 5
_CB_OPEN_SECONDS = 60
# Half-open: после истечения cb:open пропускаем не более N пробных запросов;
# успех любого — закрывает брейкер, сбой — немедленный re-open. Без этого
# весь fan-out (MultiHypothesisAgent × perspectives × retries) бил по едва
# ожившему провайдеру ОДНОВРЕМЕННО в момент истечения open-TTL.
_CB_HALF_OPEN_MAX_TRIALS = 2
_CB_HALF_OPEN_WINDOW_SECONDS = 300


class LLMResilienceManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        # Атомарный INCR fail-счётчика с ГАРАНТИРОВАННЫМ TTL. Прежний код
        # (incr, потом expire только при count==1) имел дыру: смерть процесса
        # между командами оставляла cb:fail:* БЕЗ TTL навсегда — после чего
        # каждый 5-й накопленный сбой открывал брейкер. Lua выполняется
        # атомарно; ветка TTL<0 дополнительно лечит уже протёкшие ключи.
        self.fail_incr_lua = """
        local count = redis.call('INCR', KEYS[1])
        if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
            redis.call('EXPIRE', KEYS[1], ARGV[1])
        end
        return count
        """
        # Атомарная проверка состояния брейкера с half-open trial-квотой:
        #   cb:open существует        → 1 (открыт, fail fast)
        #   cb:halfopen отсутствует   → 0 (закрыт, обычный режим)
        #   иначе — half-open: INCR trial-счётчика; первые max_trials
        #   запросов пропускаем (0), остальные отбиваем (1).
        self.circuit_state_lua = """
        if redis.call('EXISTS', KEYS[1]) == 1 then
            return 1
        end
        if redis.call('EXISTS', KEYS[2]) == 0 then
            return 0
        end
        local trials = redis.call('INCR', KEYS[3])
        if trials == 1 or redis.call('TTL', KEYS[3]) < 0 then
            redis.call('EXPIRE', KEYS[3], ARGV[2])
        end
        if trials > tonumber(ARGV[1]) then
            return 1
        end
        return 0
        """
        self.rate_limit_lua = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local fill_rate = tonumber(ARGV[2])
        local now = tonumber(ARGV[3])
        local requested = tonumber(ARGV[4])

        local bucket = redis.call('hmget', key, 'tokens', 'last_updated')
        local tokens = tonumber(bucket[1]) or capacity
        local last_updated = tonumber(bucket[2]) or now

        local delta = math.max(0, now - last_updated)
        tokens = math.min(capacity, tokens + delta * fill_rate)

        if tokens >= requested then
            tokens = tokens - requested
            redis.call('hmset', key, 'tokens', tokens, 'last_updated', now)
            return 1
        else
            return 0
        end
        """

    async def check_rate_limit(self, user_id: str) -> bool:
        key = f"rl:user:{user_id}"
        now = time.time()
        allowed = await self.redis.eval(self.rate_limit_lua, 1, key, 10, 0.1, now, 1)
        return bool(allowed)

    @staticmethod
    def _keys(provider: str) -> Tuple[str, str, str, str]:
        return (
            f"cb:open:{provider}",
            f"cb:halfopen:{provider}",
            f"cb:trial:{provider}",
            f"cb:fail:{provider}",
        )

    async def is_circuit_open(self, provider: str) -> bool:
        """True — вызов провайдера сейчас запрещён.

        Три состояния: closed (обычный режим) / open (fail fast, TTL 60s) /
        half-open (после истечения open пропускаем ограниченное число
        пробных запросов — см. circuit_state_lua). NB: в half-open каждый
        вызов этого метода потребляет trial-слот.
        """
        open_key, halfopen_key, trial_key, _ = self._keys(provider)
        state = await self.redis.eval(
            self.circuit_state_lua,
            3,
            open_key,
            halfopen_key,
            trial_key,
            _CB_HALF_OPEN_MAX_TRIALS,
            _CB_OPEN_SECONDS,
        )
        return bool(int(state))

    async def _open_circuit(self, provider: str) -> None:
        """Открыть брейкер: open на 60s + пометка half-open «на потом»."""
        open_key, halfopen_key, trial_key, fail_key = self._keys(provider)
        logger.error("circuit_breaker_opened", provider=provider)
        async with self.redis.pipeline(transaction=True) as pipe:
            await pipe.set(open_key, "1", ex=_CB_OPEN_SECONDS)
            # halfopen живёт дольше open: когда open истечёт, брейкер попадёт
            # в half-open вместо мгновенного полного закрытия.
            await pipe.set(
                halfopen_key,
                "1",
                ex=_CB_OPEN_SECONDS + _CB_HALF_OPEN_WINDOW_SECONDS,
            )
            await pipe.delete(trial_key)
            await pipe.delete(fail_key)
            await pipe.execute()

    async def report_failure(self, provider: str):
        open_key, halfopen_key, trial_key, fail_key = self._keys(provider)
        # Сбой пробного запроса в half-open → немедленный re-open, счётчик
        # заново копить не нужно (провайдер очевидно ещё не ожил).
        if not await self.redis.exists(open_key) and await self.redis.exists(
            halfopen_key
        ):
            await self._open_circuit(provider)
            return
        count = await self.redis.eval(
            self.fail_incr_lua, 1, fail_key, _CB_FAIL_WINDOW_SECONDS
        )
        if int(count) >= _CB_FAIL_THRESHOLD:
            await self._open_circuit(provider)

    async def report_success(self, provider: str):
        _, halfopen_key, trial_key, fail_key = self._keys(provider)
        # Успех закрывает брейкер полностью (в т.ч. из half-open).
        await self.redis.delete(fail_key, halfopen_key, trial_key)
