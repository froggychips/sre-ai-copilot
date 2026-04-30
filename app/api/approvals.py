from fastapi import APIRouter, HTTPException, Depends
from app.services.approval_manager import ApprovalManager
from app.celery_worker import redis_client

router = APIRouter()
approval_manager = ApprovalManager(redis_client)

@router.post("/{approval_id}/approve")
async def approve_action(approval_id: str):
    status = await approval_manager.get_status(approval_id)
    if status == "EXPIRED":
        raise HTTPException(status_code=404, detail="Approval request expired or not found")
    
    await approval_manager.approve(approval_id)
    return {"message": "Action approved", "id": approval_id}

@router.post("/{approval_id}/reject")
async def reject_action(approval_id: str):
    status = await approval_manager.get_status(approval_id)
    if status == "EXPIRED":
        raise HTTPException(status_code=404, detail="Approval request expired or not found")
        
    await approval_manager.reject(approval_id)
    return {"message": "Action rejected", "id": approval_id}

@router.get("/{approval_id}")
async def get_approval_details(approval_id: str):
    details = await approval_manager.get_details(approval_id)
    if not details:
        raise HTTPException(status_code=404, detail="Not found")
    return details
