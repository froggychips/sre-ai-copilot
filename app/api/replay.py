from fastapi import APIRouter, HTTPException
from app.database import SessionLocal, IncidentRecord
from app.celery_worker import generate_reply
import uuid

router = APIRouter()

@router.post("/{incident_id}")
async def replay_incident(incident_id: str):
    """
    Запускает повторный анализ инцидента на основе исторических данных.
    """
    db = SessionLocal()
    # Ищем инцидент по ID
    record = db.query(IncidentRecord).filter(IncidentRecord.incident_id == incident_id).first()
    
    if not record:
        db.close()
        raise HTTPException(status_code=404, detail="Incident not found in history")
    
    # Запускаем задачу в Celery с флагом replay_mode=True
    task = generate_reply.delay(
        conversation_id=str(record.incident_id), 
        prompt="Replaying historical incident analysis", 
        replay_mode=True
    )
    
    db.close()
    return {
        "status": "replay_started",
        "task_id": task.id,
        "incident_id": incident_id,
        "note": "Replay mode active: Discord notifications and K8s changes are blocked."
    }
