"""Service health score — composite metric из KG signals.

Per ChatGPT review #4.3: `kg_fragile_top` сейчас ранжирует ТОЛЬКО по
inbound count, что даёт bias на NATS clusters (93+ callers). Реальная
hierarchy «что в danger» требует composite сигнал.

Score [0, 1], 1.0 = perfect health, 0.0 = down/broken. Деривируется
из:
  - active alerts на сервисе (critical / warning, recently fired)
  - pod_events с высоким count (chronic crashloop)
  - recurrence_24h (повторяющиеся в окне 24h)

Намеренно НЕ включает:
  - is_synthetic (synthetic-узлы пропускаются sync'ом)
  - inbound count (это уже отдельная метрика fragility)
  - deploy frequency (хорошие сервисы могут редко катиться)

Refresh периодический (beat task `kg_health_recompute`), хранится в
kg_services.health_score + health_computed_at.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import AlertEvent, PodEvent, Service

log = logging.getLogger(__name__)

# Penalty веса (subtract from 1.0):
_PENALTY_PER_OPEN_CRITICAL = 0.40
_PENALTY_PER_OPEN_WARNING = 0.15
_PENALTY_CHRONIC_POD_EVENT = 0.35     # pod_event count > 1000
_PENALTY_RECURRENCE_PER_5 = 0.10       # каждые 5 recurrence в 24h

# Окна:
_OPEN_ALERT_LOOKBACK_HOURS = 24    # «активный alert» = fired за 24h без resolve
_POD_EVENT_LOOKBACK_DAYS = 7
_RECURRENCE_WINDOW_HOURS = 24


def compute_health_for_service(
    db: Session,
    service: Service,
    now: Optional[datetime] = None,
) -> Tuple[float, Dict[str, int]]:
    """Compute health [0, 1] для одного service. Returns (score, signal_counts)
    для logging / digest. Signals содержит {open_critical, open_warning,
    chronic_pod_events, recurrence_24h}.
    """
    now = now or datetime.utcnow()
    cutoff_alerts = now - timedelta(hours=_OPEN_ALERT_LOOKBACK_HOURS)
    cutoff_events = now - timedelta(days=_POD_EVENT_LOOKBACK_DAYS)
    cutoff_recur = now - timedelta(hours=_RECURRENCE_WINDOW_HOURS)

    # Открытые alerts (firing without resolved_at в окне)
    open_alerts = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.service_id == service.id,
            AlertEvent.fired_at >= cutoff_alerts,
            AlertEvent.resolved_at.is_(None),
        )
        .all()
    )
    open_critical = sum(1 for a in open_alerts if (a.severity or "").lower() == "critical")
    open_warning = sum(1 for a in open_alerts if (a.severity or "").lower() == "warning")

    # Chronic pod_events (BackOff/Unhealthy с count > 1000 — несколько суток
    # крашится; см. наш bot-service кейс с count=11789)
    chronic_pod_events = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == service.id,
            PodEvent.first_seen >= cutoff_events,
            PodEvent.count > 1000,
        )
        .count()
    )

    # Recurrence в 24h — сколько раз alert на сервисе фейерил
    recurrence_24h = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.service_id == service.id,
            AlertEvent.fired_at >= cutoff_recur,
        )
        .count()
    )

    score = 1.0
    score -= open_critical * _PENALTY_PER_OPEN_CRITICAL
    score -= open_warning * _PENALTY_PER_OPEN_WARNING
    score -= chronic_pod_events * _PENALTY_CHRONIC_POD_EVENT
    score -= (recurrence_24h // 5) * _PENALTY_RECURRENCE_PER_5
    score = max(0.0, min(1.0, score))

    signals = {
        "open_critical": open_critical,
        "open_warning": open_warning,
        "chronic_pod_events": chronic_pod_events,
        "recurrence_24h": recurrence_24h,
    }
    return score, signals


def recompute_all_health(db: Session) -> Dict[str, int]:
    """Beat task entry — пересчитать health_score для ВСЕХ real services.
    Synthetic пропускаются (по дизайну нет meaningful health).

    Returns stats: {real_services, recomputed, low_health, perfect_health}.
    """
    now = datetime.utcnow()
    services = db.query(Service).filter(Service.synthetic.is_(False)).all()
    low_health = 0
    perfect_health = 0
    recomputed = 0
    for svc in services:
        score, _ = compute_health_for_service(db, svc, now=now)
        svc.health_score = score
        svc.health_computed_at = now
        recomputed += 1
        if score < 0.4:
            low_health += 1
        if score >= 0.99:
            perfect_health += 1
    db.commit()
    log.info(
        "kg_health.recompute_done real=%d low_health=%d perfect=%d",
        len(services), low_health, perfect_health,
    )
    return {
        "real_services": len(services),
        "recomputed": recomputed,
        "low_health": low_health,
        "perfect_health": perfect_health,
    }


def top_unhealthy(
    db: Session,
    limit: int = 10,
    team: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Returns top-N сервисов с самым низким health_score.

    Используется в kg_fragile_top «честное» ранжирование и в daily digest
    секции «🩺 Top unhealthy services».
    """
    q = db.query(Service).filter(
        Service.synthetic.is_(False),
        Service.health_score.isnot(None),
    )
    if team:
        q = q.filter(Service.team_owner == team)
    rows = (
        q.order_by(Service.health_score.asc()).limit(limit).all()
    )
    return [
        {
            "namespace": s.namespace,
            "name": s.name,
            "team_owner": s.team_owner,
            "health_score": s.health_score,
            "computed_at": s.health_computed_at,
        }
        for s in rows
    ]
