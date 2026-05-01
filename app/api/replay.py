from fastapi import APIRouter, HTTPException
from app.database import SessionLocal, IncidentRecord
from app.celery_worker import generate_reply
from app.snapshot.schema import build_snapshot_from_incident
from app.replay.contract import build_environment_fingerprint, assert_replay_inputs
from app.config import settings
from app.snapshot.validator import validate_snapshot_model

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
    
    snapshot = build_snapshot_from_incident(
        incident_id=str(record.incident_id),
        incident_data=record.data or {},
        model_version=settings.MODEL_NAME,
        runtime_version="worker-v1",
        policy_decisions={"replay_mode": True},
    )
    validation = validate_snapshot_model(snapshot)
    if validation["status"] == "FAIL":
        db.close()
        raise HTTPException(status_code=422, detail={"message": "Snapshot validation failed", "validation": validation})

    if validation["status"] == "DEGRADED":
        snapshot.policy_decisions["low_fidelity_mode"] = True

    env_fp = build_environment_fingerprint({"model_name": settings.MODEL_NAME, "safe_mode": settings.SAFE_MODE})

    # Запускаем задачу в Celery с флагом replay_mode=True и immutable snapshot input
    task = generate_reply.delay(
        conversation_id=str(record.incident_id),
        prompt="Replaying historical incident analysis",
        replay_mode=True,
        snapshot=snapshot.model_dump(),
        environment_fingerprint=env_fp,
    )
    
    db.close()
    return {
        "status": "replay_started",
        "task_id": task.id,
        "incident_id": incident_id,
        "note": "Replay mode active: Discord notifications and K8s changes are blocked.",
        "snapshot_validation": validation,
    }


@router.post("/by-snapshot")
async def replay_by_snapshot(snapshot_id: str | None = None, snapshot_uri: str | None = None):
    """Contract endpoint: replay may start only with snapshot_id/snapshot_uri."""
    try:
        assert_replay_inputs(snapshot_id=snapshot_id, snapshot_uri=snapshot_uri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "accepted", "snapshot_id": snapshot_id, "snapshot_uri": snapshot_uri}
