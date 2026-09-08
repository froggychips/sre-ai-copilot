"""Действия внешних исполнителей (squad-medic) как события графа.

Приём: `POST /webhooks/remediation` (см. `api/webhooks.py`) — HMAC-SHA256
над `timestamp.body`, timestamp обязателен (окно `REMEDIATION_WEBHOOK_MAX_AGE_SECONDS`),
anti-replay по подписи. Схема та же, что у AlertManager-вебхука, чтобы у
второго внешнего писателя не появилось второй схемы аутентификации.

Запись: одно событие на прогон исполнителя по стенду; повтор того же
`(actor, run_id, namespace)` — идемпотентный no-op (retry исполнителя).
Событие привязывается к открытому инциденту графа по любому из namespace
стенда — так действие медика попадает в timeline инцидента и в
операционную память рядом с решениями собственного executor'а.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, cast

import structlog
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_naive
from app.knowledge_graph.schema import KGIncident, KGRemediationEvent
from app.models.remediation_event import RemediationEventIn
from app.security.replay import is_timestamp_fresh

log = structlog.get_logger()

SIGNATURE_HEADER = "X-Remediation-Signature"
TIMESTAMP_HEADER = "X-Remediation-Timestamp"


class SignatureError(ValueError):
    """Причина отказа — в сообщении; HTTP-слой мапит в 401."""


def check_remediation_signature(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: Optional[str],
    max_age_seconds: int,
    now: Optional[float] = None,
) -> str:
    """Проверить подпись запроса; вернуть hex-подпись (ключ anti-replay).

    Чистая функция без FastAPI — её тестируют напрямую, HTTP-обёртка только
    поднимает HTTPException. Fail-closed: без секрета — отказ, без timestamp —
    отказ (в отличие от AlertManager, здесь нет legacy body-only пути).
    """
    if not secret:
        raise SignatureError("remediation webhook authentication is not configured")
    lookup = {k.lower(): v for k, v in headers.items()}
    signature = (lookup.get(SIGNATURE_HEADER.lower()) or "").strip()
    timestamp = (lookup.get(TIMESTAMP_HEADER.lower()) or "").strip()
    if not signature:
        raise SignatureError("missing signature")
    if not timestamp:
        raise SignatureError("missing timestamp")
    if signature.startswith("sha256="):
        signature = signature[len("sha256="):]
    if not is_timestamp_fresh(timestamp, max_age_seconds, now=now):
        raise SignatureError("stale timestamp")
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    # Байты, не str: Starlette декодирует заголовки как latin-1, и не-ASCII в
    # подписи ронял compare_digest(str, str) TypeError-ом → 500 вместо 401.
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
        raise SignatureError("invalid signature")
    return signature


def open_incident_for_namespaces(
    db: Session, namespaces: List[str], *, at: Optional[datetime] = None,
) -> Optional[KGIncident]:
    """Самый свежий инцидент по любому namespace стенда, открытый на момент `at`.

    Резолвнутый до `at` инцидент не подходит (действие было после); открытый
    позже `at` — тоже (действие его не касалось).
    """
    if not namespaces:
        return None
    at_n = ensure_naive(at or datetime.utcnow())
    q = db.query(KGIncident).filter(
        KGIncident.namespace.in_(namespaces),
        KGIncident.opened_at <= at_n,
    )
    rows = q.order_by(KGIncident.opened_at.desc()).limit(20).all()
    for inc in rows:
        resolved = cast(Optional[datetime], inc.resolved_at)
        if inc.status == "open" or (resolved is not None and ensure_naive(resolved) >= at_n):
            return inc
    return None


def record_external_remediation(db: Session, payload: RemediationEventIn) -> Dict[str, Any]:
    """Сохранить событие; повтор (actor, run_id, namespace) — вернуть существующее.

    Возвращает {"id", "incident_id", "created": bool}.
    """
    if payload.run_id:
        existing = (
            db.query(KGRemediationEvent)
            .filter(
                KGRemediationEvent.actor == payload.actor,
                KGRemediationEvent.run_id == payload.run_id,
                KGRemediationEvent.namespace == payload.namespace,
            )
            .one_or_none()
        )
        if existing is not None:
            return {"id": existing.id, "incident_id": existing.incident_id, "created": False}

    all_ns = list(payload.namespaces)
    if payload.namespace not in all_ns:
        all_ns.insert(0, payload.namespace)
    started = ensure_naive(payload.started_at)
    finished = ensure_naive(payload.finished_at) if payload.finished_at else None
    incident = open_incident_for_namespaces(db, all_ns, at=finished or started)

    row = KGRemediationEvent(
        actor=payload.actor,
        run_id=payload.run_id,
        namespace=payload.namespace,
        namespaces=all_ns,
        squad=payload.squad,
        service_name=payload.service_name or (incident.service_name if incident is not None else None),
        started_at=started,
        finished_at=finished,
        duration_min=payload.duration_min,
        outcome=payload.outcome,
        severity=payload.severity,
        fixed=payload.fixed,
        still_unhealthy=payload.still_unhealthy,
        applied=list(payload.applied),
        manual=list(payload.manual),
        gaps=list(payload.gaps),
        summary=payload.summary,
        root_cause=payload.root_cause,
        next_action=payload.next_action,
        escalated=payload.escalated,
        owner_login=payload.owner_login,
        incident_id=incident.id if incident is not None else None,
        extras=payload.extras,
    )
    db.add(row)
    db.commit()
    log.info(
        "remediation_event.recorded", actor=payload.actor, squad=payload.squad,
        namespace=payload.namespace, outcome=payload.outcome,
        kg_incident_id=row.incident_id, applied=len(payload.applied), manual=len(payload.manual),
    )
    return {"id": row.id, "incident_id": row.incident_id, "created": True}


def external_events_for_timeline(
    db: Session, incident: KGIncident, start: datetime, end: datetime,
) -> List[KGRemediationEvent]:
    """События внешних исполнителей для timeline инцидента: привязанные к нему
    напрямую ИЛИ по его namespace в окне."""
    start_n, end_n = ensure_naive(start), ensure_naive(end)
    # Небольшой запас: медик пишет started_at начала лечения, а инцидент мог
    # открыться на минуту позже — событие всё равно про этот стенд.
    lo = start_n - timedelta(minutes=5)
    rows = (
        db.query(KGRemediationEvent)
        .filter(
            (KGRemediationEvent.incident_id == incident.id)
            | (
                (KGRemediationEvent.namespace == incident.namespace)
                & (KGRemediationEvent.started_at >= lo)
                & (KGRemediationEvent.started_at <= end_n)
            )
        )
        .order_by(KGRemediationEvent.started_at)
        .all()
    )
    return rows


def event_to_dict(row: KGRemediationEvent) -> Dict[str, Any]:
    return {
        "id": row.id, "actor": row.actor, "run_id": row.run_id,
        "squad": row.squad, "namespace": row.namespace, "namespaces": row.namespaces or [],
        "service_name": row.service_name,
        "started_at": row.started_at, "finished_at": row.finished_at, "duration_min": row.duration_min,
        "outcome": row.outcome, "severity": row.severity,
        "fixed": bool(row.fixed), "still_unhealthy": bool(row.still_unhealthy),
        "applied": row.applied or [], "manual": row.manual or [], "gaps": row.gaps or [],
        "summary": row.summary, "root_cause": row.root_cause, "next_action": row.next_action,
        "escalated": bool(row.escalated), "owner_login": row.owner_login,
        "incident_id": row.incident_id, "created_at": row.created_at,
    }
