import json
import uuid
import structlog
from typing import Optional, Dict, Any
from redis.asyncio import Redis

logger = structlog.get_logger()

class ApprovalManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.prefix = "sre:approval:"

    async def request_approval(self, user_id: str, operation_details: Dict[str, Any], ttl: int = 1800) -> str:
        """
        Создает запрос на подтверждение операции. Живет 30 минут по умолчанию.
        """
        approval_id = str(uuid.uuid4())
        data = {
            "id": approval_id,
            "status": "PENDING",
            "user_id": user_id,
            "details": operation_details,
            "created_at": str(uuid.uuid1().time)
        }
        key = f"{self.prefix}{approval_id}"
        await self.redis.set(key, json.dumps(data), ex=ttl)
        logger.info("approval_request_created", approval_id=approval_id, risk=operation_details.get("risk"))
        return approval_id

    async def approve(self, approval_id: str):
        key = f"{self.prefix}{approval_id}"
        raw = await self.redis.get(key)
        if raw:
            data = json.loads(raw)
            data["status"] = "APPROVED"
            # Даем короткое окно (5 мин) для воркера, чтобы подхватить аппрув
            await self.redis.set(key, json.dumps(data), ex=300)
            logger.info("approval_status_updated", approval_id=approval_id, status="APPROVED")

    async def reject(self, approval_id: str):
        key = f"{self.prefix}{approval_id}"
        raw = await self.redis.get(key)
        if raw:
            data = json.loads(raw)
            data["status"] = "REJECTED"
            await self.redis.set(key, json.dumps(data), ex=300)
            logger.info("approval_status_updated", approval_id=approval_id, status="REJECTED")

    async def get_status(self, approval_id: str) -> str:
        key = f"{self.prefix}{approval_id}"
        raw = await self.redis.get(key)
        if not raw:
            return "EXPIRED"
        return json.loads(raw)["status"]

    async def get_details(self, approval_id: str) -> Optional[Dict[str, Any]]:
        key = f"{self.prefix}{approval_id}"
        raw = await self.redis.get(key)
        return json.loads(raw) if raw else None
