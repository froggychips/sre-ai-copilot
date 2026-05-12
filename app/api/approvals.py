from fastapi import APIRouter, Depends, HTTPException

from app.auth import User, get_current_user
from app.celery_worker import redis_client
from app.services.approval_manager import ApprovalManager

router = APIRouter()
approval_manager = ApprovalManager(redis_client)


@router.post("/{approval_id}/approve")
async def approve_action(approval_id: str, user: User = Depends(get_current_user)):
    # Check if user has approval role
    if "approver" not in user.roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    status = await approval_manager.get_status(approval_id)
    if status == "EXPIRED":
        raise HTTPException(
            status_code=404, detail="Approval request expired or not found"
        )

    await approval_manager.approve(approval_id)
    return {"message": "Action approved", "id": approval_id}


@router.post("/{approval_id}/reject")
async def reject_action(approval_id: str, user: User = Depends(get_current_user)):
    if "approver" not in user.roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    status = await approval_manager.get_status(approval_id)
    if status == "EXPIRED":
        raise HTTPException(
            status_code=404, detail="Approval request expired or not found"
        )

    await approval_manager.reject(approval_id)
    return {"message": "Action rejected", "id": approval_id}


@router.get("/{approval_id}")
async def get_approval_details(
    approval_id: str, user: User = Depends(get_current_user)
):
    details = await approval_manager.get_details(approval_id)
    if not details:
        raise HTTPException(status_code=404, detail="Not found")
    return details
