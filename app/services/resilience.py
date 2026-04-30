import time
import structlog
from redis.asyncio import Redis
from fastapi import HTTPException
from app.config import settings

logger = structlog.get_logger()

class LLMResilienceManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        # Атомарный Token Bucket на Lua
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
        # Лимиты из конфига: например, 5 запросов в минуту
        allowed = await self.redis.eval(self.rate_limit_lua, 1, key, 10, 0.1, now, 1)
        return bool(allowed)

    async def is_circuit_open(self, provider: str) -> bool:
        return await self.redis.exists(f"cb:open:{provider}")

    async def report_failure(self, provider: str):
        key = f"cb:fail:{provider}"
        count = await self.redis.incr(key)
        if count == 1: await self.redis.expire(key, 60)
        
        if count >= 5: # Порог срабатывания
            logger.error("circuit_breaker_opened", provider=provider)
            await self.redis.set(f"cb:open:{provider}", "1", ex=60)

    async def report_success(self, provider: str):
        await self.redis.delete(f"cb:fail:{provider}")
