import json
import uuid
import structlog
from typing import List, Dict, Any, Optional
from redis.asyncio import Redis

logger = structlog.get_logger()

class SessionManager:
    def __init__(self, redis_client: Redis, ttl: int = 86400):
        self.redis = redis_client
        self.ttl = ttl
        # Префиксы для организации пространства ключей
        self.PRE_HISTORY = "sre:session:history:"
        self.PRE_STATE = "sre:session:state:"
        self.PRE_META = "sre:session:meta:"

    def _get_key(self, prefix: str, session_id: str) -> str:
        return f"{prefix}{session_id}"

    async def create_session(self, user_id: str) -> str:
        """Создает сессию и сохраняет метаданные."""
        session_id = str(uuid.uuid4())
        meta = {"user_id": user_id, "type": "incident_analysis"}
        key = self._get_key(self.PRE_META, session_id)
        await self.redis.set(key, json.dumps(meta), ex=self.ttl)
        logger.info("session_created", session_id=session_id, user_id=user_id)
        return session_id

    async def add_message(self, session_id: str, role: str, content: str):
        """Добавляет сообщение в историю (Redis List) с продлением TTL."""
        key = self._get_key(self.PRE_HISTORY, session_id)
        message = json.dumps({"role": role, "content": content})
        
        async with self.redis.pipeline(transaction=True) as pipe:
            await pipe.rpush(key, message)
            await pipe.expire(key, self.ttl)
            await pipe.execute()
        
        logger.debug("message_added_to_history", session_id=session_id, role=role)

    async def get_context(self, session_id: str, last_n: int = 15) -> List[Dict[str, str]]:
        """Возвращает последние N сообщений для промпта."""
        key = self._get_key(self.PRE_HISTORY, session_id)
        raw_messages = await self.redis.lrange(key, -last_n, -1)
        return [json.loads(m) for m in raw_messages]

    async def save_agent_state(self, session_id: str, agent_id: str, state: Dict[str, Any]):
        """Сохраняет промежуточное состояние конкретного агента."""
        key = self._get_key(self.PRE_STATE, session_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            await pipe.hset(key, agent_id, json.dumps(state))
            await pipe.expire(key, self.ttl)
            await pipe.execute()
        
        logger.info("agent_state_persisted", session_id=session_id, agent=agent_id)

    async def get_agent_state(self, session_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """Получает состояние агента из Redis."""
        key = self._get_key(self.PRE_STATE, session_id)
        raw_state = await self.redis.hget(key, agent_id)
        return json.loads(raw_state) if raw_state else None
