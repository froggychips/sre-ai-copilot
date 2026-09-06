"""Инцидент как детерминированный объект из алертов сервиса.

Без LLM и без эвристик «примерно одновременно». Правило одно: алерт
относится к сервису, у сервиса не больше одного открытого инцидента, значит
алерт относится к нему. Корреляция МЕЖДУ сервисами (общая база, деплой в
окне, вызов через `calls`) — отдельный шаг roadmap, и он придёт сюда как
рёбра между инцидентами, а не как размытие этого правила.

Жизненный цикл:

    алерт fired ──▶ attach_alert ──▶ открытый инцидент сервиса есть? ──да──▶ присоединить
                                            │нет
                                            ▼
                              закрыт < REOPEN_WINDOW_MIN назад? ──да──▶ переоткрыть
                                            │нет
                                            ▼
                                        завести новый

    reconcile_incidents (beat, 5 мин): все алерты resolved → resolved;
    давно ни алерта, ни резолва → aged_out.

Почему переоткрытие, а не новый инцидент: флаппинг (алерт гаснет и через
минуту загорается) для триажа — одно событие; десять инцидентов за час по
одному сервису — шум, который прячет настоящее.

Почему `aged_out`: `alerts_resolve_sync` закрывает алерты по firing-набору
Alertmanager, но при недоступном AM сохраняет их открытыми (safety). Без
старения такой инцидент висел бы вечно и блокировал бы заведение нового.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

import structlog
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_naive
from app.knowledge_graph.schema import AlertEvent, KGIncident

log = structlog.get_logger("kg.incidents")

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

RESOLVE_ALL_ALERTS = "all_alerts_resolved"
RESOLVE_AGED_OUT = "aged_out"

#: Алерт после закрытия инцидента в этом окне переоткрывает его.
REOPEN_WINDOW_MIN = 30
#: Открытый инцидент без единого нового алерта столько часов — состарить.
AGE_OUT_HOURS = 7 * 24

_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _now() -> datetime:
    return datetime.utcnow()


def incident_key(namespace: str, service_name: str, opened_at: datetime) -> str:
    return f"{namespace}/{service_name}@{opened_at:%Y%m%dT%H%M%S}"


def _max_severity(a: Optional[str], b: Optional[str]) -> Optional[str]:
    ra = _SEVERITY_RANK.get((a or "").lower(), 0)
    rb = _SEVERITY_RANK.get((b or "").lower(), 0)
    if ra == 0 and rb == 0:
        return a or b
    return a if ra >= rb else b


def open_incident_for(db: Session, namespace: str, service_name: str) -> Optional[KGIncident]:
    return (
        db.query(KGIncident)
        .filter(
            KGIncident.namespace == namespace,
            KGIncident.service_name == service_name,
            KGIncident.status == STATUS_OPEN,
        )
        .one_or_none()
    )


def _recently_resolved(
    db: Session, namespace: str, service_name: str, fired_at: datetime,
) -> Optional[KGIncident]:
    since = fired_at - timedelta(minutes=REOPEN_WINDOW_MIN)
    return (
        db.query(KGIncident)
        .filter(
            KGIncident.namespace == namespace,
            KGIncident.service_name == service_name,
            KGIncident.status == STATUS_RESOLVED,
            KGIncident.resolved_at.isnot(None),
            KGIncident.resolved_at >= since,
        )
        .order_by(KGIncident.resolved_at.desc())
        .first()
    )


def attach_alert(
    db: Session,
    *,
    namespace: str,
    service_name: str,
    fired_at: datetime,
    alertname: str,
    severity: Optional[str] = None,
    fingerprint: Optional[str] = None,
    service_id: Optional[int] = None,
) -> KGIncident:
    """Присоединить алерт к инциденту сервиса (открытому, переоткрытому или новому).

    Идемпотентно по fingerprint: повторный fire того же алерта не меняет
    `alert_count`. Проставляет `kg_alerts.incident_id = incident_key`, если
    строка алерта уже записана (вызывать ПОСЛЕ record_alert_event).
    """
    fired_at = ensure_naive(fired_at)
    inc = open_incident_for(db, namespace, service_name)
    action = "attached"

    if inc is None:
        inc = _recently_resolved(db, namespace, service_name, fired_at)
        if inc is not None:
            reopened: Any = inc
            reopened.status = STATUS_OPEN
            reopened.resolved_at = None
            reopened.resolve_reason = None
            reopened.reopened_count = (inc.reopened_count or 0) + 1
            action = "reopened"

    if inc is None:
        inc = KGIncident(
            incident_key=incident_key(namespace, service_name, fired_at),
            namespace=namespace,
            service_name=service_name,
            service_id=service_id,
            status=STATUS_OPEN,
            severity=severity,
            opened_at=fired_at,
            last_alert_at=fired_at,
            alert_count=0,
            alertnames=[],
            fingerprints=[],
            reopened_count=0,
        )
        db.add(inc)
        action = "opened"

    # SQLAlchemy Column[...] без Mapped[]: присваиваем через Any-вид, как в
    # alerts_resolve_sync (cast(Any, ...)) — иначе mypy спорит с каждой строкой.
    row: Any = inc
    # JSON-колонки: присваиваем НОВЫЙ список — мутация in-place SQLAlchemy
    # без MutableList не заметит.
    fps: List[str] = list(inc.fingerprints or [])
    if fingerprint and fingerprint not in fps:
        fps.append(fingerprint)
    row.fingerprints = fps
    names: List[str] = list(inc.alertnames or [])
    if alertname and alertname not in names:
        names.append(alertname)
    row.alertnames = names
    row.alert_count = len(fps) if fps else (inc.alert_count or 0) + 1
    row.severity = _max_severity(cast(Optional[str], inc.severity), severity)
    if fired_at > inc.last_alert_at:
        row.last_alert_at = fired_at
    if fired_at < inc.opened_at:
        # Алерт пришёл с опозданием и был раньше первого — окно честнее
        # расширить, ключ при этом не трогаем: на него уже ссылаются.
        row.opened_at = fired_at
    if service_id and not inc.service_id:
        row.service_id = service_id
    db.flush()

    if fingerprint:
        db.query(AlertEvent).filter(AlertEvent.fingerprint == fingerprint).update(
            {"incident_id": inc.incident_key}, synchronize_session=False,
        )

    log.info(
        "kg.incident." + action,
        incident=inc.incident_key, alertname=alertname, alerts=inc.alert_count,
    )
    return inc


def reconcile_incidents(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Закрыть инциденты, у которых все алерты resolved; состарить брошенные.

    Возвращает счётчики для контракта source_status: `checked` — сколько
    открытых просмотрено (observed), остальное — что с ними стало.
    """
    now = ensure_naive(now or _now())
    stats: Dict[str, Any] = {"checked": 0, "still_open": 0, "resolved": 0, "aged_out": 0}
    open_incidents = db.query(KGIncident).filter(KGIncident.status == STATUS_OPEN).all()
    age_limit = now - timedelta(hours=AGE_OUT_HOURS)

    for inc in open_incidents:
        row: Any = inc
        stats["checked"] += 1
        fps = list(inc.fingerprints or [])
        if fps:
            rows = (
                db.query(AlertEvent.fingerprint, AlertEvent.resolved_at)
                .filter(AlertEvent.fingerprint.in_(fps))
                .all()
            )
            seen = {fp for fp, _ in rows}
            still_firing = [fp for fp, r in rows if r is None]
            missing = set(fps) - seen
            if not still_firing and not missing:
                row.status = STATUS_RESOLVED
                row.resolved_at = max(r for _, r in rows)
                row.resolve_reason = RESOLVE_ALL_ALERTS
                stats["resolved"] += 1
                continue
        if inc.last_alert_at < age_limit:
            row.status = STATUS_RESOLVED
            row.resolved_at = now
            row.resolve_reason = RESOLVE_AGED_OUT
            stats["aged_out"] += 1
            continue
        stats["still_open"] += 1

    db.commit()
    if stats["resolved"] or stats["aged_out"]:
        log.info("kg.incidents.reconciled", **stats)
    return stats


def incident_to_dict(inc: KGIncident) -> Dict[str, Any]:
    return {
        "id": inc.id,
        "incident_key": inc.incident_key,
        "namespace": inc.namespace,
        "service": inc.service_name,
        "service_id": inc.service_id,
        "status": inc.status,
        "severity": inc.severity,
        "opened_at": inc.opened_at,
        "last_alert_at": inc.last_alert_at,
        "resolved_at": inc.resolved_at,
        "resolve_reason": inc.resolve_reason,
        "alert_count": inc.alert_count,
        "alertnames": list(inc.alertnames or []),
        "fingerprints": list(inc.fingerprints or []),
        "reopened_count": inc.reopened_count or 0,
    }
