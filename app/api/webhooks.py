from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.workers.tasks import process_incident_task, celery_app
from app.database import get_db, IncidentRecord
from app.models.incident import AlertManagerWebhook, Incident
from app.ingestion.raw_collector import raw_collector

router = APIRouter()


@router.post("/alertmanager", status_code=202)
async def alertmanager_webhook(payload: AlertManagerWebhook, db: Session = Depends(get_db)):
    """Receive a Prometheus AlertManager webhook batch and dispatch one Celery task per alert."""
    raw_collector.ingest(payload.model_dump())

    accepted = []
    for alert in payload.alerts:
        incident = Incident.from_alertmanager(alert)
        existing = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident.incident_id)
            .first()
        )
        if existing is None:
            db.add(
                IncidentRecord(
                    incident_id=incident.incident_id,
                    status="PENDING",
                    data=incident.model_dump(),
                )
            )
        task = process_incident_task.delay(incident.model_dump())
        accepted.append({"incident_id": incident.incident_id, "task_id": task.id})

    db.commit()
    return {"status": "accepted", "alerts": accepted}


@router.get("/status/{task_id}")
async def get_task_status(task_id: str):
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "status": res.status}
