import hashlib
import structlog
from typing import Optional, Callable
from functools import wraps
from redis.asyncio import Redis

logger = structlog.get_logger()

class LLMCache:
    def __init__(self, redis_client: Redis, ttl: int = 3600):
        self.redis = redis_client
        self.ttl = ttl
        self.prefix = "llm:cache:"

    def _generate_key(self, role: str, instruction: str, context: str) -> str:
        """
        Создает уникальный SHA-256 ключ на основе полного контекста запроса.
        """
        raw_string = f"{role}:{instruction}:{context}"
        hash_digest = hashlib.sha256(raw_string.encode("utf-8")).hexdigest()
        return f"{self.prefix}{hash_digest}"

    async def get(self, role: str, instruction: str, context: str) -> Optional[str]:
        key = self._generate_key(role, instruction, context)
        cached = await self.redis.get(key)
        if cached:
            logger.info("llm_cache_hit", key=key[:12])
            return cached.decode("utf-8")
        return None

    async def set(self, role: str, instruction: str, context: str, response: str):
        key = self._generate_key(role, instruction, context)
        await self.redis.set(key, response, ex=self.ttl)
        logger.info("llm_cache_miss_saved", key=key[:12])

def cache_llm_response(ttl: int = 3600):
    """
    Декоратор для методов класса BaseAgent, который кэширует ответы в Redis.
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, user_context: str, instruction: str = "", **kwargs):
            # Используем Redis клиент, который должен быть инициализирован в системе
            from app.celery_worker import redis_client
            cache_manager = LLMCache(redis_client, ttl=ttl)
            
            # 1. Пробуем получить из кэша
            cached_result = await cache_manager.get(self.role, instruction, user_context)
            if cached_result:
                return cached_result
            
            # 2. Если нет — вызываем реальный метод (с LLM, Guardrails и т.д.)
            response = await func(self, user_context, instruction, **kwargs)
            
            # 3. Сохраняем в кэш
            await cache_manager.set(self.role, instruction, user_context, response)
            return response
        return wrapper
    return decorator
