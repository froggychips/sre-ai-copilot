import hashlib
import hmac
import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.core.state_machine import IncidentState
from app.database import IncidentRecord, get_db
from app.ingestion.raw_collector import raw_collector
from app.models.incident import AlertManagerWebhook, Incident
from app.services.teamcity_service import incident_teamcity_context
from app.workers.tasks import (async_process_incident, celery_app,
                               process_incident_task)

router = APIRouter()
log = structlog.get_logger()


async def verify_alertmanager_signature(request: Request):
    """Verify HMAC-SHA256 signature on AlertManager webhook.

    В prod settings гарантирует наличие ALERTMANAGER_WEBHOOK_SECRET (см.
    Settings._enforce_prod_invariants). В dev без секрета вызов пропускается
    с предупреждением — чтобы локальный e2e не требовал ключа, но шумел в логах.
    """
    if not settings.ALERTMANAGER_WEBHOOK_SECRET:
        log.warning(
            "alertmanager.webhook_unauthenticated",
            env=settings.ENV,
            reason="ALERTMANAGER_WEBHOOK_SECRET not set",
        )
        return

    signature = request.headers.get("X-Alertmanager-Signature")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing AlertManager signature")

    # AlertManager-совместимый формат: либо голый hex, либо `sha256=<hex>`.
    if signature.startswith("sha256="):
        signature = signature[len("sha256="):]

    body = await request.body()
    expected_signature = hmac.new(
        settings.ALERTMANAGER_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid AlertManager signature")


def validate_alert_labels(alert):
    """Validate alert labels for security."""
    labels = alert.labels
    alertname = labels.get("alertname")
    namespace = labels.get("namespace")

    if not alertname:
        raise HTTPException(status_code=400, detail="Missing alertname in labels")

    # Allowlist for alertname - adjust as needed
    allowed_alertnames = {
        "PodCrashLooping",
        "HighCpuUsage",
        "MemoryPressure",
        "DeploymentUnavailable",
    }
    if alertname not in allowed_alertnames:
        log.warning("unknown_alertname", alertname=alertname)
        # For now, allow but log; in production, reject

    if namespace:
        # Basic validation - no special chars that could be used for injection
        if not re.match(r"^[a-z0-9-]+$", namespace):
            raise HTTPException(status_code=400, detail="Invalid namespace format")


@router.post(
    "/alertmanager",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """Receive a Prometheus AlertManager webhook batch and dispatch one Celery task per alert."""
    # raw_collector requires `id` or `incident_id` at top level. AlertManager
    # batch не имеет глобального id, поэтому используем `groupKey` как идентификатор
    # этого batch-а (один webhook = один batch с одним groupKey).
    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    # States where the pipeline has already run or is in flight — no retry.
    # FAILED is the only terminal state we allow to re-run (transient infra error).
    _SKIP_STATES = {
        IncidentState.OPEN.value,              # queued, pipeline hasn't started yet
        IncidentState.INVESTIGATING.value,
        IncidentState.FACTS_COLLECTED.value,
        IncidentState.HYPOTHESIS_GENERATED.value,
        IncidentState.FIX_PROPOSED.value,
        IncidentState.RESOLVED.value,
    }

    accepted = []
    for alert in payload.alerts:
        validate_alert_labels(alert)
        incident = Incident.from_alertmanager(alert)

        # Dedup check BEFORE TC enrichment — no point spending the TC roundtrip
        # if we're going to skip this fingerprint anyway.
        existing = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident.incident_id)
            .first()
        )
        if existing is not None and existing.status in _SKIP_STATES:
            log.info(
                "webhook.deduplicated",
                incident_id=incident.incident_id,
                status=existing.status,
            )
            accepted.append({
                "incident_id": incident.incident_id,
                "task_id": "deduplicated",
                "status": existing.status,
            })
            continue

        # TC enrichment — best-effort, only for new / retriable incidents.
        try:
            incident.teamcity_context = await incident_teamcity_context(
                namespace=incident.namespace,
                incident_starts_at=incident.starts_at,
            )
        except Exception as e:
            log.warning(
                "teamcity_context.unhandled",
                error=str(e),
                incident_id=incident.incident_id,
            )

        if existing is None:
            db.add(
                IncidentRecord(
                    incident_id=incident.incident_id,
                    status=IncidentState.OPEN.value,
                    data=incident.model_dump(),
                )
            )
        else:
            # FAILED retry: reset to OPEN so the state machine can advance again.
            existing.status = IncidentState.OPEN.value
        # Commit before pipeline: the worker opens its own SessionLocal and needs
        # the row to be visible before it starts writing state transitions.
        db.commit()

        if settings.PIPELINE_DIRECT_INVOKE:
            await async_process_incident(incident.model_dump())
            accepted.append({"incident_id": incident.incident_id, "task_id": "direct"})
        else:
            task = process_incident_task.delay(incident.model_dump())
            accepted.append({"incident_id": incident.incident_id, "task_id": task.id})

    return {"status": "accepted", "alerts": accepted}


@router.get("/status/{task_id}")
async def get_task_status(task_id: str):
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "status": res.status}
