from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import structlog
from app.workers.tasks import process_incident_task, celery_app
from app.database import get_db, IncidentRecord
from app.models.incident import AlertManagerWebhook, Incident
from app.ingestion.raw_collector import raw_collector
from app.services.teamcity_service import incident_teamcity_context

router = APIRouter()
log = structlog.get_logger()


@router.post("/alertmanager", status_code=202)
async def alertmanager_webhook(payload: AlertManagerWebhook, db: Session = Depends(get_db)):
    """Receive a Prometheus AlertManager webhook batch and dispatch one Celery task per alert."""
    raw_collector.ingest(payload.model_dump())

    accepted = []
    for alert in payload.alerts:
        incident = Incident.from_alertmanager(alert)
        # Обогащение TC-контекстом (recent deploys + changes) — best-effort.
        # При недоступности TC просто остаётся None, инцидент обрабатывается без контекста.
        try:
            incident.teamcity_context = await incident_teamcity_context(
                namespace=incident.namespace,
                incident_starts_at=incident.starts_at,
            )
        except Exception as e:
            log.warning("teamcity_context.unhandled", error=str(e), incident_id=incident.incident_id)
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
