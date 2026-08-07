from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, HTTPException

from app.auth import User, get_current_user
from app.celery_worker import redis_client
from app.services.approval_manager import ApprovalManager

if TYPE_CHECKING:
    from redis.asyncio import Redis

router = APIRouter()
# redis_client — LoopLocalRedis-прокси (клиент per event loop); статически это
# не Redis, хотя делегирует ему все атрибуты. См. app/services/resilience.py.
approval_manager = ApprovalManager(cast("Redis", redis_client))


def _require_approver(user: User) -> None:
    if "approver" not in user.roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def _raise_transition_failure(current_status: str) -> None:
    """Атомарный переход не случился — отдать честный статус-код.

    Раньше approve/reject игнорировали None от approval_manager и всегда
    отвечали 200 «Action approved/rejected» — оператор, «отклонивший» уже
    одобренное действие, был уверен, что оно отклонено.
    """
    if current_status == "EXPIRED":
        raise HTTPException(
            status_code=404, detail="Approval request expired or not found"
        )
    raise HTTPException(
        status_code=409,
        detail=f"Approval is not pending (current status: {current_status})",
    )


@router.post("/{approval_id}/approve")
async def approve_action(approval_id: str, user: User = Depends(get_current_user)):
    # Check if user has approval role
    _require_approver(user)

    new_status = await approval_manager.approve(approval_id)
    if new_status is None:
        _raise_transition_failure(await approval_manager.get_status(approval_id))
    return {"message": "Action approved", "id": approval_id, "status": new_status}


@router.post("/{approval_id}/reject")
async def reject_action(approval_id: str, user: User = Depends(get_current_user)):
    _require_approver(user)

    new_status = await approval_manager.reject(approval_id)
    if new_status is None:
        _raise_transition_failure(await approval_manager.get_status(approval_id))
    return {"message": "Action rejected", "id": approval_id, "status": new_status}


@router.get("/{approval_id}")
async def get_approval_details(
    approval_id: str, user: User = Depends(get_current_user)
):
    # Детали содержат предложенную kubectl-команду и risk — те же данные,
    # которые охраняет роль approver на approve/reject. Без проверки роли
    # endpoint отдавал их любому аутентифицированному пользователю.
    _require_approver(user)
    details = await approval_manager.get_details(approval_id)
    if not details:
        raise HTTPException(status_code=404, detail="Not found")
    return details
