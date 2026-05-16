import asyncio
import inspect
import time
from functools import wraps
from typing import Any, Callable, Tuple, Type

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger()


def llm_retry_strategy(func):
    """Simple async retry decorator for transient LLM failures."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        retries = 3
        delay = 0.5
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("llm_call_retry", attempt=attempt, error=str(exc))
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
        raise last_exc

    return wrapper


def with_external_retry(
    *,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
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
        По дефолту любая Exception. Узкий список (TimeoutError,
        ConnectionError) — для случаев когда нужно НЕ ретраить
        bug-and-permanent-fail-ы.
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


class LLMResilienceManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
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

    async def is_circuit_open(self, provider: str) -> bool:
        return await self.redis.exists(f"cb:open:{provider}")

    async def report_failure(self, provider: str):
        key = f"cb:fail:{provider}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 60)

        if count >= 5:
            logger.error("circuit_breaker_opened", provider=provider)
            await self.redis.set(f"cb:open:{provider}", "1", ex=60)

    async def report_success(self, provider: str):
        await self.redis.delete(f"cb:fail:{provider}")
