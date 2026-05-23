"""Compute per-service агрегаты сигналов из KG → kg_signal_aggregates.

Beat-task `kg_signal_aggregates_compute` (раз в час). НЕ ходит во внешние
API — читает kg_deployments / kg_alerts / kg_pod_events за окно window_hours
и пишет одну строку per real service.

Поля:
  * deploy_count          — количество deploys в окне.
  * deploy_failure_pct    — % FAILED / TOTAL (0.0 если deploy_count=0).
  * alert_open_count      — alerts с fired_at в окне и resolved_at IS NULL.
  * alert_ttr_p50_min     — медиана (resolved_at - fired_at) для resolved.
  * pod_event_count       — sum(count) для pod_events в окне.
  * top_event_reason      — самый частый reason.
  * slo_burn_pct          — упрощённо `open_critical / max(1, deploy_count)`.

Идемпотентность: UNIQUE(service_id, window_end). Один и тот же tick (округлён
до часа) не плодит дубли. Если строка уже есть — пропускаем (не обновляем —
в окне в любом случае уже всё посчитано).
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (AlertEvent, Deployment, PodEvent,
                                        Service, SignalAggregate)

log = logging.getLogger(__name__)


def _round_to_hour(dt: datetime) -> datetime:
    """Округление вниз до часа — стабильный window_end для idempotency."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _compute_for_service(
    db: Session,
    service: Service,
    window_start: datetime,
    window_end: datetime,
) -> Dict[str, Any]:
    # ── Deployments ────────────────────────────────────────────────────
    deploys: List[Deployment] = (
        db.query(Deployment)
        .filter(
            Deployment.service_id == service.id,
            Deployment.started_at >= window_start,
            Deployment.started_at < window_end,
        )
        .all()
    )
    deploy_count = len(deploys)
    failed = sum(
        1 for d in deploys
        if (d.status or "").upper() in ("FAILURE", "FAILED", "ERROR")
    )
    deploy_failure_pct = (
        (failed / deploy_count) * 100.0 if deploy_count else 0.0
    )

    # ── Alerts ─────────────────────────────────────────────────────────
    alerts: List[AlertEvent] = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.service_id == service.id,
            AlertEvent.fired_at >= window_start,
            AlertEvent.fired_at < window_end,
        )
        .all()
    )
    alert_open_count = sum(1 for a in alerts if a.resolved_at is None)
    open_critical = sum(
        1 for a in alerts
        if a.resolved_at is None and (a.severity or "").lower() == "critical"
    )
    ttr_minutes: List[float] = []
    for a in alerts:
        if a.resolved_at and a.fired_at:
            delta = (a.resolved_at - a.fired_at).total_seconds() / 60.0
            if delta >= 0:
                ttr_minutes.append(delta)
    alert_ttr_p50_min: Optional[float] = (
        statistics.median(ttr_minutes) if ttr_minutes else None
    )

    # ── Pod events ─────────────────────────────────────────────────────
    pod_events: List[PodEvent] = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == service.id,
            PodEvent.first_seen >= window_start,
            PodEvent.first_seen < window_end,
        )
        .all()
    )
    pod_event_count = sum((e.count or 1) for e in pod_events)
    top_event_reason: Optional[str] = None
    if pod_events:
        counter: Counter = Counter()
        for e in pod_events:
            counter[e.reason] += int(e.count or 1)
        top_event_reason, _ = counter.most_common(1)[0]

    # ── SLO burn (упрощённая модель) ───────────────────────────────────
    # Кричащих критов больше чем deploys → burn > 100%. Без deploys — open_critical
    # делим на 1 (deploys=0 значит сервис не катился, любой open_critical = burn).
    slo_burn_pct = (
        (open_critical / max(1, deploy_count)) * 100.0
    )

    return {
        "deploy_count": deploy_count,
        "deploy_failure_pct": round(deploy_failure_pct, 2),
        "alert_open_count": alert_open_count,
        "alert_ttr_p50_min": (
            round(alert_ttr_p50_min, 2) if alert_ttr_p50_min is not None else None
        ),
        "pod_event_count": pod_event_count,
        "top_event_reason": top_event_reason,
        "slo_burn_pct": round(slo_burn_pct, 2),
    }


def _insert_idempotent(
    db: Session,
    *,
    service_id: int,
    window_end: datetime,
    window_hours: int,
    values: Dict[str, Any],
) -> bool:
    row = SignalAggregate(
        service_id=service_id,
        window_end=window_end,
        window_hours=window_hours,
        **values,
    )
    try:
        with db.begin_nested():
            db.add(row)
        return True
    except IntegrityError:
        return False


def compute_signal_aggregates(
    db: Session,
    window_hours: int = 24,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Beat-task entry — посчитать aggregates для всех real services.

    `now` — для тестов (фиксированное время). Default = datetime.utcnow().
    """
    now = now or datetime.utcnow()
    window_end = _round_to_hour(now)
    window_start = window_end - timedelta(hours=window_hours)

    services: List[Service] = (
        db.query(Service).filter(Service.synthetic.is_(False)).all()
    )
    stats: Dict[str, Any] = {
        "real_services": len(services),
        "window_hours": window_hours,
        "window_end": window_end.isoformat(),
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_empty": 0,
        "errors": 0,
    }

    for svc in services:
        try:
            values = _compute_for_service(db, svc, window_start, window_end)
        except Exception as e:
            stats["errors"] += 1
            log.warning(
                "signal_aggregates.compute_failed ns=%s name=%s err=%s",
                svc.namespace, svc.name, e,
            )
            continue

        # Skip полностью пустых сервисов (нет ни deploys, ни alerts, ни events
        # в окне). Защита от взрывного роста таблицы под ~370 real services × 24h.
        if (
            values["deploy_count"] == 0
            and values["alert_open_count"] == 0
            and values["pod_event_count"] == 0
        ):
            stats["skipped_empty"] += 1
            continue

        ok = _insert_idempotent(
            db,
            service_id=cast(int, svc.id),
            window_end=window_end,
            window_hours=window_hours,
            values=values,
        )
        if ok:
            stats["inserted"] += 1
        else:
            stats["skipped_dup"] += 1

    db.commit()
    log.info(
        "signal_aggregates.done real=%d window=%dh inserted=%d "
        "skipped_dup=%d skipped_empty=%d errors=%d",
        stats["real_services"], window_hours, stats["inserted"],
        stats["skipped_dup"], stats["skipped_empty"], stats["errors"],
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(compute_signal_aggregates(db))
    finally:
        db.close()
