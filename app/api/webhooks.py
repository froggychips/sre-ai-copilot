from fastapi import APIRouter, Request, Depends, HTTPException
from app.workers.tasks import process_incident_task
from app.database import get_db, IncidentRecord
from sqlalchemy.orm import Session
from app.config import settings

router = APIRouter()

@router.post("/newrelic", status_code=202)
async def newrelic_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    incident_id = data.get("incident_id")
    
    # Persist initial record
    new_incident = IncidentRecord(
        incident_id=incident_id,
        status="PENDING",
        data=data
    )
    db.add(new_incident)
    db.commit()

    # Async task
    task = process_incident_task.delay(data)
    
    return {
        "status": "accepted",
        "task_id": task.id,
        "location": f"/webhooks/status/{task.id}"
    }

@router.get("/status/{task_id}")
async def get_task_status(task_id: str):
    from app.workers.tasks import celery_app
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "status": res.status}
