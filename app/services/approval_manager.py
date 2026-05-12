import json
import uuid
from typing import Any, Dict, Optional

import structlog
from redis.asyncio import Redis

from app.services.telemetry_utils import approval_span

logger = structlog.get_logger()


# Атомарный переход статуса PENDING → APPROVED/REJECTED.
# Раньше approve()/reject() делали get→mutate→set без блокировки —
# параллельный double-click в Discord или race approve+reject портили state.
# Lua-скрипт исполняется атомарно в Redis (single-thread).
#
# KEYS[1] = ключ записи
# ARGV[1] = целевой статус (APPROVED/REJECTED)
# ARGV[2] = TTL (сек) после смены
# Returns:
#   <new status>                                — success
#   error "NOT_FOUND"                           — ключ истёк или не было
#   error "DECODE"                              — повреждённое значение
#   error "NOT_PENDING:<status>"                — уже терминальное состояние
_TRANSITION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return redis.error_reply('NOT_FOUND')
end
local ok, data = pcall(cjson.decode, raw)
if not ok then
    return redis.error_reply('DECODE')
end
if data['status'] ~= 'PENDING' then
    return redis.error_reply('NOT_PENDING:' .. data['status'])
end
data['status'] = ARGV[1]
local new_raw = cjson.encode(data)
redis.call('SET', KEYS[1], new_raw, 'EX', ARGV[2])
return data['status']
"""


class ApprovalManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.prefix = "sre:approval:"
        self._transition = self.redis.register_script(_TRANSITION_LUA)

    async def request_approval(
        self, user_id: str, operation_details: Dict[str, Any], ttl: int = 1800
    ) -> str:
        """Создаёт запрос на подтверждение. Живёт 30 минут по умолчанию."""
        approval_id = str(uuid.uuid4())
        data = {
            "id": approval_id,
            "status": "PENDING",
            "user_id": user_id,
            "details": operation_details,
            "created_at": str(uuid.uuid1().time),
        }
        key = f"{self.prefix}{approval_id}"
        with approval_span(approval_id, "request", user_id=user_id,
                           risk=str(operation_details.get("risk", ""))):
            await self.redis.set(key, json.dumps(data), ex=ttl)
            logger.info(
                "approval_request_created",
                approval_id=approval_id,
                risk=operation_details.get("risk"),
            )
        return approval_id

    async def _atomic_transition(self, approval_id: str, target: str) -> Optional[str]:
        """Возвращает новый статус или None, если переход невозможен."""
        key = f"{self.prefix}{approval_id}"
        with approval_span(approval_id, target.lower()) as span:
            try:
                result = await self._transition(keys=[key], args=[target, 300])
            except Exception as e:  # redis.exceptions.ResponseError несёт текст ошибки
                err = str(e)
                if err in ("NOT_FOUND", "DECODE") or err.startswith("NOT_PENDING:"):
                    span.add_event("approval.transition_rejected", attributes={"reason": err})
                    logger.warning(
                        "approval_transition_rejected",
                        approval_id=approval_id,
                        target=target,
                        reason=err,
                    )
                    return None
                raise
            new_status = result.decode() if isinstance(result, bytes) else result
            span.set_attribute("sre.approval.status", new_status)
            logger.info(
                "approval_status_updated", approval_id=approval_id, status=new_status
            )
        return new_status

    async def approve(self, approval_id: str) -> Optional[str]:
        return await self._atomic_transition(approval_id, "APPROVED")

    async def reject(self, approval_id: str) -> Optional[str]:
        return await self._atomic_transition(approval_id, "REJECTED")

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
