import hashlib
import hmac
import re
from typing import Optional

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

    if namespace:
        # Basic validation — no special chars that could be used for injection
        if not re.match(r"^[a-z0-9-]+$", namespace):
            raise HTTPException(status_code=400, detail="Invalid namespace format")

    # instance label (Node* alerts) — same injection guard
    instance = labels.get("instance", "")
    if instance and not re.match(r"^[a-zA-Z0-9._:/-]+$", instance):
        raise HTTPException(status_code=400, detail="Invalid instance format")


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

    # States where the pipeline is already in-flight — skip re-dispatch.
    # FAILED is the only terminal state we allow to re-run (transient infra error).
    # NOTE: RESOLVED is intentionally absent — a re-fire after RESOLVED is flapping
    # and must be re-processed (see flapping detection below).
    _SKIP_STATES = {
        IncidentState.OPEN.value,
        IncidentState.INVESTIGATING.value,
        IncidentState.FACTS_COLLECTED.value,
        IncidentState.HYPOTHESIS_GENERATED.value,
        IncidentState.FIX_PROPOSED.value,
    }

    accepted = []
    for alert in payload.alerts:
        validate_alert_labels(alert)
        incident = Incident.from_alertmanager(alert)

        existing = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident.incident_id)
            .first()
        )

        # ── RESOLVED webhook: update DB, no pipeline dispatch ──────────────
        if alert.status == "resolved":
            if existing is not None and existing.status not in {
                IncidentState.RESOLVED.value,
                IncidentState.FAILED.value,
            }:
                existing.status = IncidentState.RESOLVED.value
                db.commit()
                log.info("webhook.alert_resolved", incident_id=incident.incident_id,
                         prev_status=existing.status)
            accepted.append({"incident_id": incident.incident_id, "task_id": "resolved"})
            continue

        # ── FIRING: detect flapping (re-fire after RESOLVED) ───────────────
        if existing is not None and existing.status == IncidentState.RESOLVED.value:
            prev_flap_count = (existing.data or {}).get("flap_count", 0)
            incident = incident.model_copy(update={"flap_count": prev_flap_count + 1})
            log.info(
                "webhook.flapping_detected",
                incident_id=incident.incident_id,
                flap_count=incident.flap_count,
            )
            # Fall through to TC enrichment + pipeline re-run.

        # ── Normal dedup: pipeline already in-flight ───────────────────────
        elif existing is not None and existing.status in _SKIP_STATES:
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
            # Reset to OPEN for both FAILED retry and flapping re-fire.
            # Overwrite data so flap_count is persisted in the row.
            existing.status = IncidentState.OPEN.value
            existing.data = incident.model_dump()
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


@router.post(
    "/alertmanager/store",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook_store_only(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """KG event-store endpoint — БЕЗ LLM-pipeline'а.

    Принимает AlertManager batch, записывает каждый alert в `kg_alerts`
    через `populate_from_incident`, ACK 202. Никакого
    `process_incident_task.delay()` — LLM-токены НЕ расходуются.

    Цель: наполнить event-store чтобы `nearby_alerts`/`incidents_on`/
    recurrence-detection заработали на live data. AlertManager рулится
    сюда; основной `/alertmanager` endpoint остаётся отключенным до
    запуска E2E-тестов и budget-cap'ов.

    HMAC-подпись и signature-check те же что и у full pipeline endpoint.
    """
    from app.knowledge_graph.auto_populator import populate_from_incident

    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    stored = []
    for alert in payload.alerts:
        try:
            validate_alert_labels(alert)
            incident = Incident.from_alertmanager(alert)
        except HTTPException:
            # Малформированные alerts skip-аем, не падаем на batch.
            log.warning("kg_store.skipped_invalid_alert", labels=alert.labels)
            continue

        # Resolved-events тоже регистрируем — даёт recurrence/flapping
        # сигнал, не только firing.
        if alert.status == "resolved":
            stored.append({"incident_id": incident.incident_id, "result": "resolved-skipped"})
            continue

        try:
            stats = populate_from_incident(db, incident)
            stored.append({"incident_id": incident.incident_id, "result": "stored", **stats})
        except Exception as e:
            log.warning(
                "kg_store.populate_failed",
                incident_id=incident.incident_id,
                error=type(e).__name__,
                message=str(e),
            )
            stored.append({"incident_id": incident.incident_id, "result": "failed"})

    db.commit()
    return {"status": "stored", "alerts": stored}


@router.post(
    "/alertmanager/enrich-and-forward",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook_enrich_and_forward(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """KG-enriched Discord-forward, БЕЗ LLM.

    Делает то же, что /store (пишет alert-event в kg_alerts), и
    дополнительно для каждого FIRING alert-а собирает KG-контекст
    (recent_deploys, nearby_alerts, recurrence, downstream, owner) и
    отправляет один embed в Discord webhook.

    Группировка: alerts с одинаковым (alertname, severity) в одном
    AM-batch сворачиваются в один embed (несколько ns в одном сообщении).
    Это снижает шум типа «3 одинаковых KubePodCrashLooping подряд».

    Если DISCORD_ENRICH_ENABLED=false — поведение идентично /store.
    """
    from app.knowledge_graph.auto_populator import populate_from_incident
    from app.services.alert_enrichment import enrich_alert
    from app.services.discord_service import DiscordService

    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    stored = []
    firing_incidents = []  # (incident, env-hint) — для post-store enrich.

    for alert in payload.alerts:
        try:
            validate_alert_labels(alert)
            incident = Incident.from_alertmanager(alert)
        except HTTPException:
            log.warning("enrich_forward.skipped_invalid_alert", labels=alert.labels)
            continue

        if alert.status == "resolved":
            # Resolved-events не идут в Discord (можно расширить позже).
            stored.append({"incident_id": incident.incident_id, "result": "resolved-skipped"})
            continue

        try:
            stats = populate_from_incident(db, incident)
            stored.append({"incident_id": incident.incident_id, "result": "stored", **stats})
        except Exception as e:
            log.warning(
                "enrich_forward.populate_failed",
                incident_id=incident.incident_id,
                error=type(e).__name__,
                message=str(e),
            )
            stored.append({"incident_id": incident.incident_id, "result": "failed"})
        firing_incidents.append(incident)

    db.commit()

    # Discord-enrich tier. Под фичефлагом — чтобы /store-style behaviour
    # сохранялся, пока канарейка не подтвердит безвредность.
    enriched_groups = 0
    if settings.DISCORD_ENRICH_ENABLED and firing_incidents:
        # Группировка по (alertname, severity) — несколько ns в одном embed.
        groups: dict[tuple, list] = {}
        for inc in firing_incidents:
            key = (
                inc.labels.get("alertname", "unknown"),
                (inc.severity or "unknown").lower(),
            )
            groups.setdefault(key, []).append(inc)

        # Hint про env берём из namespace prefix первого incident-а.
        def _env_hint(ns: Optional[str]) -> Optional[str]:
            if not ns:
                return None
            for p in ("prod", "preprod", "preupdate", "squad", "dev"):
                if ns.startswith(p + "-"):
                    return p
            return None

        discord_service = DiscordService()
        for (alertname, sev), incs in groups.items():
            try:
                ctxs = [enrich_alert(db, inc) for inc in incs]
                env_hint = _env_hint(incs[0].namespace)
                await discord_service.send_enriched_alert(ctxs, env=env_hint)
                enriched_groups += 1
            except Exception as e:
                log.warning(
                    "enrich_forward.send_failed",
                    alertname=alertname,
                    severity=sev,
                    error=type(e).__name__,
                    message=str(e),
                )

    return {
        "status": "stored-and-forwarded",
        "alerts": stored,
        "enriched_groups": enriched_groups,
        "enrich_enabled": settings.DISCORD_ENRICH_ENABLED,
    }


@router.get("/status/{task_id}")
async def get_task_status(task_id: str):
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "status": res.status}
