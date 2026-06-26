"""Service health score — composite metric из KG signals.

Per ChatGPT review #4.3: `kg_fragile_top` сейчас ранжирует ТОЛЬКО по
inbound count, что даёт bias на NATS clusters (93+ callers). Реальная
hierarchy «что в danger» требует composite сигнал.

Score [0, 1], 1.0 = perfect health, 0.0 = down/broken. Деривируется
из:
  - active alerts на сервисе (critical / warning, recently fired)
  - pod_events с высоким count (chronic crashloop)
  - recurrence_24h (повторяющиеся в окне 24h)
  - p95 latency drift (текущее окно vs baseline 7d) — penalty при +50%
  - http_5xx_rate (текущий) — penalty при > 1%
  - deploy_failure_pct из kg_signal_aggregates — penalty при > 20%
  - slo_burn_pct из kg_signal_aggregates — penalty при > 10%

Намеренно НЕ включает:
  - is_synthetic (synthetic-узлы пропускаются sync'ом)
  - inbound count (это уже отдельная метрика fragility)
  - deploy frequency (хорошие сервисы могут редко катиться)

Каждый новый компонент капается отдельным penalty cap (`_PENALTY_CAP_PER_COMPONENT`),
чтобы один сигнал не обнулял score целиком — нужно несколько параллельных
проблем чтобы дойти до 0. Если данных по метрикам нет (< _MIN_POINTS_FOR_METRICS
точек в окне) или нет свежей записи signal_aggregates (< 1h) — компонент
просто скипается (graceful degradation, не penalty).

⚠️ ОГРАНИЧЕНИЕ (частично закрыто 2026-06-10): `p95 latency` и `http_5xx_rate`
в `kg_service_health` **всё ещё всегда 0** — app `/metrics` (Kestrel) закрыт
JWT-middleware, скрейпа нет (бэкенд-тикет WO-12483). Ingress-часть закрыта:
nginx-ingress метрики собираются по всем окружениям, per-host/path 5xx и
latency живут в `kg_ingress_observations` — но это другой разрез (endpoint,
не service). Поэтому app-слойные компоненты (5xx, p95) по факту НЕ срабатывают,
и health_score деривируется из инфра/событийных сигналов (alerts +
pod_events + deploy/slo aggregates), а НЕ из user-facing latency/errors.
Не интерпретировать как «пользователю хорошо/плохо»: высокий score = «cpu/
события в норме», НЕ «нет 5xx». Реальный app-error прокси — `log_error_rate`
(`queries.log_error_rate_for`, log-derived, тоже НЕ HTTP 5xx). Канон
ограничений — `docs/KG_SCHEMA_CONTRACT.md` §Consumer caveats.

Refresh периодический (beat task `kg_health_recompute`), хранится в
kg_services.health_score + health_computed_at.

См. также `app.knowledge_graph.contract` — `is_synthetic` (этот модуль
skip-ает synthetic-узлы), `QUALITY_THRESHOLDS` (общие пороги). Формальный
контракт — `docs/KG_SCHEMA_CONTRACT.md`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (
    AlertEvent,
    IngressObservation,
    PodEvent,
    Service,
    ServiceHealth,
    SignalAggregate,
)

log = logging.getLogger(__name__)

# Penalty веса (subtract from 1.0):
_PENALTY_PER_OPEN_CRITICAL = 0.40
_PENALTY_PER_OPEN_WARNING = 0.15
_PENALTY_CHRONIC_POD_EVENT = 0.35     # pod_event count > 1000
_PENALTY_RECURRENCE_PER_5 = 0.10       # каждые 5 recurrence в 24h

# Cap для каждого «нового» компонента — ни один не должен в одиночку
# обнулить score; 0.25 = до 4 параллельных проблем чтобы дойти до 0.
_PENALTY_CAP_PER_COMPONENT = 0.25

# Trigger-пороги для новых компонентов.
_P95_DRIFT_TRIGGER_PCT = 50.0    # текущий p95 > baseline * 1.5
_HTTP_5XX_TRIGGER = 0.01          # rate > 1%
_DEPLOY_FAILURE_TRIGGER_PCT = 20.0
_SLO_BURN_TRIGGER_PCT = 10.0

# Чувствительность penalty (penalty = min(cap, weight * over_threshold)):
_P95_DRIFT_WEIGHT_PER_PCT = 0.005   # 100% drift = 0.5 нагруз, capped 0.25
_HTTP_5XX_WEIGHT = 5.0              # 5% 5xx = 0.20 (5% * 5 - порог 0.01*5)
_DEPLOY_FAILURE_WEIGHT_PER_PCT = 0.005
_SLO_BURN_WEIGHT_PER_PCT = 0.01

# Ingress-derived 5xx (kg_ingress_observations.error_5xx_rate — 5xx запросов/сек
# на ingress-границе сервиса). Живой источник (nginx-ingress) в отличие от
# per-service kg_service_health.http_5xx_rate (закрыт JWT, WO-12483, всегда 0).
# Меряет трафик НА ГРАНИЦЕ, поэтому отдельный компонент, а не подмена.
_INGRESS_5XX_TRIGGER_RPS = 0.05    # < 0.05 rps 5xx = шум, не штрафуем
_INGRESS_5XX_WEIGHT = 1.0          # 0.30 rps over → 0.30 → capped 0.25

# Окна:
_OPEN_ALERT_LOOKBACK_HOURS = 24    # «активный alert» = fired за 24h без resolve
_POD_EVENT_LOOKBACK_DAYS = 7
_RECURRENCE_WINDOW_HOURS = 24
_METRIC_RECENT_WINDOW_HOURS = 1    # «текущее» значение — последний час
_METRIC_BASELINE_WINDOW_DAYS = 7   # baseline — последние 7д для p95
_SIGNAL_AGG_FRESHNESS_HOURS = 1    # свежая запись = в пределах часа
_MIN_POINTS_FOR_METRICS = 6        # < 6 точек за час — данных мало, скипаем
_INGRESS_RECENT_WINDOW_MINUTES = 30  # окно для свежих ingress-наблюдений


# ── helper'ы для оконных метрик ──────────────────────────────────────────────

def _recent_p95_and_5xx(
    db: Session,
    service_id: int,
    now: datetime,
) -> Tuple[Optional[float], Optional[float], int]:
    """Последний час: средний p95 и средний 5xx_rate. Returns (p95, 5xx, n_points).

    Если точек меньше _MIN_POINTS_FOR_METRICS — оба значения None
    (вызывающий должен скипнуть penalty).
    """
    cutoff = now - timedelta(hours=_METRIC_RECENT_WINDOW_HOURS)
    rows = (
        db.query(ServiceHealth)
        .filter(
            ServiceHealth.service_id == service_id,
            ServiceHealth.ts >= cutoff,
        )
        .all()
    )
    if len(rows) < _MIN_POINTS_FOR_METRICS:
        return None, None, len(rows)
    p95_vals: List[float] = [
        float(r.p95_latency_ms) for r in rows if r.p95_latency_ms is not None
    ]
    err_vals: List[float] = [
        float(r.http_5xx_rate) for r in rows if r.http_5xx_rate is not None
    ]
    p95 = sum(p95_vals) / len(p95_vals) if p95_vals else None
    err = sum(err_vals) / len(err_vals) if err_vals else None
    return p95, err, len(rows)


def _baseline_p95(
    db: Session,
    service_id: int,
    now: datetime,
) -> Optional[float]:
    """Baseline p95 за 7д — средний (не median, упрощённо). None если данных нет."""
    cutoff_lo = now - timedelta(days=_METRIC_BASELINE_WINDOW_DAYS)
    cutoff_hi = now - timedelta(hours=_METRIC_RECENT_WINDOW_HOURS)
    rows = (
        db.query(ServiceHealth)
        .filter(
            ServiceHealth.service_id == service_id,
            ServiceHealth.ts >= cutoff_lo,
            ServiceHealth.ts < cutoff_hi,
        )
        .all()
    )
    vals: List[float] = [
        float(r.p95_latency_ms) for r in rows if r.p95_latency_ms is not None
    ]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _recent_ingress_5xx_p95(
    db: Session,
    service_id: int,
    now: datetime,
) -> Tuple[Optional[float], Optional[float]]:
    """Пиковые ingress-derived 5xx-rate (rps) и p95 (ms) за окно.

    Источник — kg_ingress_observations (nginx-ingress, per host/path). Берём
    ПОСЛЕДНЮЮ запись на каждый endpoint в окне и возвращаем (max 5xx, max p95).
    None,None если наблюдений нет (сервис без Ingress'а / нет данных).
    """
    cutoff = now - timedelta(minutes=_INGRESS_RECENT_WINDOW_MINUTES)
    rows = (
        db.query(IngressObservation)
        .filter(
            IngressObservation.service_id == service_id,
            IngressObservation.ts >= cutoff,
        )
        .order_by(IngressObservation.ts.desc())
        .all()
    )
    if not rows:
        return None, None
    latest: Dict[Tuple[Any, Any], IngressObservation] = {}
    for r in rows:
        key = (r.host, r.path)
        if key not in latest:
            latest[key] = r
    obs = list(latest.values())
    err_vals = [float(o.error_5xx_rate) for o in obs if o.error_5xx_rate is not None]
    p95_vals = [float(o.p95_latency_ms) for o in obs if o.p95_latency_ms is not None]
    max_err = max(err_vals) if err_vals else None
    max_p95 = max(p95_vals) if p95_vals else None
    return max_err, max_p95


def _latest_signal_aggregate(
    db: Session,
    service_id: int,
    now: datetime,
) -> Optional[SignalAggregate]:
    """Свежая (window_end >= now - 1h) запись из kg_signal_aggregates.

    Если её нет — None (вызывающий скипает deploy_failure / slo_burn).
    """
    cutoff = now - timedelta(hours=_SIGNAL_AGG_FRESHNESS_HOURS)
    return (
        db.query(SignalAggregate)
        .filter(
            SignalAggregate.service_id == service_id,
            SignalAggregate.window_end >= cutoff,
        )
        .order_by(SignalAggregate.window_end.desc())
        .first()
    )


def _capped(value: float) -> float:
    """Limit penalty одного компонента."""
    return min(_PENALTY_CAP_PER_COMPONENT, max(0.0, value))


def compute_health_for_service(
    db: Session,
    service: Service,
    now: Optional[datetime] = None,
) -> Tuple[float, Dict[str, Optional[float]]]:
    """Compute health [0, 1] для одного service. Returns (score, signal_counts)
    для logging / digest. Signals содержит {open_critical, open_warning,
    chronic_pod_events, recurrence_24h, p95_drift_pct, http_5xx_rate,
    ingress_5xx_rate, ingress_p95_ms, deploy_failure_pct, slo_burn_pct}.

    Формула:
      score = 1.0
        - open_critical * 0.40
        - open_warning  * 0.15
        - chronic_pod_events * 0.35
        - (recurrence_24h // 5) * 0.10
        - p95_drift_penalty       (capped 0.25)
        - http_5xx_penalty        (capped 0.25)  # per-service, 0 пока WO-12483
        - ingress_5xx_penalty     (capped 0.25)  # ingress-derived, живой
        - deploy_failure_penalty  (capped 0.25)
        - slo_burn_penalty        (capped 0.25)
      clamp [0, 1]
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

    # ── новые компоненты ────────────────────────────────────────────────
    p95_recent, err_recent, n_points = _recent_p95_and_5xx(
        db, cast(int, service.id), now
    )

    # p95 drift: текущий vs baseline, penalty при > +50%
    p95_drift_pct: Optional[float] = None
    p95_drift_penalty = 0.0
    if p95_recent is not None:
        baseline = _baseline_p95(db, cast(int, service.id), now)
        if baseline and baseline > 0:
            drift = (p95_recent - baseline) / baseline * 100.0
            p95_drift_pct = drift
            if drift > _P95_DRIFT_TRIGGER_PCT:
                over = drift - _P95_DRIFT_TRIGGER_PCT
                p95_drift_penalty = _capped(over * _P95_DRIFT_WEIGHT_PER_PCT)

    # 5xx rate: penalty при > 1%
    http_5xx_penalty = 0.0
    if err_recent is not None and err_recent > _HTTP_5XX_TRIGGER:
        over = err_recent - _HTTP_5XX_TRIGGER
        http_5xx_penalty = _capped(over * _HTTP_5XX_WEIGHT)

    # ingress-derived 5xx (живой источник nginx-ingress; per-service http_5xx
    # выше всегда 0 — app /metrics за JWT, WO-12483). Отдельный компонент:
    # меряет 5xx-rps НА ГРАНИЦЕ сервиса. p95 — информационный (нет baseline'а
    # для drift'а на ingress-разрезе), штрафуем только 5xx.
    ingress_5xx_rate, ingress_p95_ms = _recent_ingress_5xx_p95(
        db, cast(int, service.id), now
    )
    ingress_5xx_penalty = 0.0
    if ingress_5xx_rate is not None and ingress_5xx_rate > _INGRESS_5XX_TRIGGER_RPS:
        over = ingress_5xx_rate - _INGRESS_5XX_TRIGGER_RPS
        ingress_5xx_penalty = _capped(over * _INGRESS_5XX_WEIGHT)

    # deploy_failure_pct / slo_burn_pct — из свежей записи kg_signal_aggregates
    agg = _latest_signal_aggregate(db, cast(int, service.id), now)
    deploy_failure_pct: Optional[float] = None
    slo_burn_pct: Optional[float] = None
    deploy_failure_penalty = 0.0
    slo_burn_penalty = 0.0
    if agg is not None:
        deploy_failure_pct = agg.deploy_failure_pct
        slo_burn_pct = agg.slo_burn_pct
        if deploy_failure_pct is not None and deploy_failure_pct > _DEPLOY_FAILURE_TRIGGER_PCT:
            over = deploy_failure_pct - _DEPLOY_FAILURE_TRIGGER_PCT
            deploy_failure_penalty = _capped(over * _DEPLOY_FAILURE_WEIGHT_PER_PCT)
        if slo_burn_pct is not None and slo_burn_pct > _SLO_BURN_TRIGGER_PCT:
            over = slo_burn_pct - _SLO_BURN_TRIGGER_PCT
            slo_burn_penalty = _capped(over * _SLO_BURN_WEIGHT_PER_PCT)

    score = 1.0
    score -= open_critical * _PENALTY_PER_OPEN_CRITICAL
    score -= open_warning * _PENALTY_PER_OPEN_WARNING
    score -= chronic_pod_events * _PENALTY_CHRONIC_POD_EVENT
    score -= (recurrence_24h // 5) * _PENALTY_RECURRENCE_PER_5
    score -= p95_drift_penalty
    score -= http_5xx_penalty
    score -= ingress_5xx_penalty
    score -= deploy_failure_penalty
    score -= slo_burn_penalty
    score = max(0.0, min(1.0, score))

    signals = {
        "open_critical": open_critical,
        "open_warning": open_warning,
        "chronic_pod_events": chronic_pod_events,
        "recurrence_24h": recurrence_24h,
        "p95_drift_pct": p95_drift_pct,
        "http_5xx_rate": err_recent,
        "ingress_5xx_rate": ingress_5xx_rate,
        "ingress_p95_ms": ingress_p95_ms,
        "deploy_failure_pct": deploy_failure_pct,
        "slo_burn_pct": slo_burn_pct,
    }

    # Tracability — breakdown что сколько съело (DEBUG-only, чтоб не шумел)
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "kg_health.breakdown svc=%s/%s score=%.3f "
            "crit=%.2f warn=%.2f chronic=%.2f recur=%.2f "
            "p95=%.3f 5xx=%.3f deploy_fail=%.3f slo_burn=%.3f "
            "metric_points=%d agg=%s",
            service.namespace, service.name, score,
            open_critical * _PENALTY_PER_OPEN_CRITICAL,
            open_warning * _PENALTY_PER_OPEN_WARNING,
            chronic_pod_events * _PENALTY_CHRONIC_POD_EVENT,
            (recurrence_24h // 5) * _PENALTY_RECURRENCE_PER_5,
            p95_drift_penalty, http_5xx_penalty,
            deploy_failure_penalty, slo_burn_penalty,
            n_points, "yes" if agg is not None else "no",
        )

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
