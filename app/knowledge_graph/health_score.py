"""Service health score — composite metric из KG signals.

Per ChatGPT review #4.3: `kg_fragile_top` сейчас ранжирует ТОЛЬКО по
inbound count, что даёт bias на NATS clusters (93+ callers). Реальная
hierarchy «что в danger» требует composite сигнал.

Score [0, 1], 1.0 = perfect health, 0.0 = down/broken. Деривируется
из:
  - open alerts на сервисе (critical / warning, resolved_at IS NULL —
    БЕЗ окна по fired_at, см. `_OPEN_ALERT_MAX_AGE_DAYS`)
  - pod_events с высоким count (chronic crashloop) — окно по last_seen
  - recurrence_24h (повторяющиеся в окне 24h)
  - p95 latency drift (текущее окно vs baseline 7d) — penalty при +50%
  - http_5xx_rate (текущий) — penalty при > 0.05 rps 5xx (единицы — rps,
    НЕ доля; см. `_HTTP_5XX_TRIGGER_RPS`)
  - deploy_failure_pct из kg_signal_aggregates — penalty при > 20%
  - slo_burn_pct из kg_signal_aggregates — penalty при > 10%

Намеренно НЕ включает:
  - is_synthetic (synthetic-узлы пропускаются sync'ом)
  - inbound count (это уже отдельная метрика fragility)
  - deploy frequency (хорошие сервисы могут редко катиться)

Каждый новый компонент капается отдельным penalty cap (`_PENALTY_CAP_PER_COMPONENT`),
чтобы один сигнал не обнулял score целиком — нужно несколько параллельных
проблем чтобы дойти до 0. Если данных по метрикам нет (< _MIN_POINTS_FOR_METRICS
точек в окне) или нет свежей записи signal_aggregates (см.
`_SIGNAL_AGG_FRESHNESS_HOURS` — допуск привязан к расписанию ЗАПИСИ
агрегатов, не к расписанию пересчёта) — компонент просто скипается
(graceful degradation, не penalty).

⚠️ ОГРАНИЧЕНИЕ (частично закрыто 2026-06-10): `p95 latency` и `http_5xx_rate`
в `kg_service_health` **пишутся как NULL** (не 0 — проверено 14.08.2026 на
2.8 млн строк за 10 суток) — app `/metrics` (Kestrel) закрыт
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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.knowledge_graph.namespace_lifecycle import missing_namespace_names
from app.knowledge_graph.schema import (
    NODE_KIND_SERVICE,
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
# http_5xx_rate из kg_service_health — это `sum(rate(http_requests_total
# {status=~"5.."}))`, т.е. 5xx-ЗАПРОСОВ В СЕКУНДУ, а не доля от трафика
# (metrics_sync._q_ns_5xx_by_service). Доли посчитать нечем: знаменателя
# (общий rps) в kg_service_health нет вообще. Прежний порог 0.01 с
# комментарием «rate > 1%» путал единицы: как только скрейп откроют
# (WO-12483), штраф начинался бы с 0.01 rps (1 ошибка в 100 секунд) и
# упирался в cap уже на 0.06 rps — то есть на любом ненулевом error-трафике.
# Приводим к честным rps и к той же шкале, что у ingress-компонента
# (_INGRESS_5XX_TRIGGER_RPS/_INGRESS_5XX_WEIGHT): один и тот же сигнал на
# двух слоях должен штрафовать одинаково. 0.05 rps = 3 ошибки в минуту —
# выше шума одиночных ретраев; cap 0.25 достигается на 0.30 rps.
_HTTP_5XX_TRIGGER_RPS = 0.05
_DEPLOY_FAILURE_TRIGGER_PCT = 20.0
_SLO_BURN_TRIGGER_PCT = 10.0

# Чувствительность penalty (penalty = min(cap, weight * over_threshold)):
_P95_DRIFT_WEIGHT_PER_PCT = 0.005   # 100% drift = 0.5 нагруз, capped 0.25
_HTTP_5XX_WEIGHT_PER_RPS = 1.0      # 0.30 rps over → 0.30 → capped 0.25
_DEPLOY_FAILURE_WEIGHT_PER_PCT = 0.005
_SLO_BURN_WEIGHT_PER_PCT = 0.01

# Ingress-derived 5xx (kg_ingress_observations.error_5xx_rate — 5xx запросов/сек
# на ingress-границе сервиса). Живой источник (nginx-ingress) в отличие от
# per-service kg_service_health.http_5xx_rate (закрыт JWT, WO-12483, NULL).
# Меряет трафик НА ГРАНИЦЕ, поэтому отдельный компонент, а не подмена.
_INGRESS_5XX_TRIGGER_RPS = 0.05    # < 0.05 rps 5xx = шум, не штрафуем
_INGRESS_5XX_WEIGHT = 1.0          # 0.30 rps over → 0.30 → capped 0.25

# Окна:
# «Открытый alert» = resolved_at IS NULL, БЕЗ окна по fired_at. Раньше окно
# было 24h и это делало health_score слепым ровно на хронические инциденты:
# медиана TTR у KubeDeploymentReplicasMismatch 29h, p90 = 83h (TTR-аналитика
# по kg_alerts, см. stuck_alerts.py), а record_alert_event для ongoing-алерта
# СОХРАНЯЕТ исходный fired_at (populator.py: on-conflict не двигает fired_at
# пока строка не resolved). Итог: critical, горящий вторые сутки, выпадал из
# alert-penalty → score возвращался к ~1.0 в разгар многодневной аварии и
# сервис исчезал из top_unhealthy.
# Верхняя граница остаётся только как защита от «фантомов» сломанного
# resolve-пути: 30д = то же окно, в котором kg_alerts_resolve_sync резолвит
# по AM-снимку (alerts_resolve_sync.py), а их залипание — это сигнал
# check_alerts_resolve_freshness, не health_score.
_OPEN_ALERT_MAX_AGE_DAYS = 30
_POD_EVENT_LOOKBACK_DAYS = 7
_RECURRENCE_WINDOW_HOURS = 24      # окно ТОЛЬКО для recurrence-подсчётов
_METRIC_RECENT_WINDOW_HOURS = 1    # «текущее» значение — последний час
_METRIC_BASELINE_WINDOW_DAYS = 7   # baseline — последние 7д для p95
# Freshness агрегатов kg_signal_aggregates. Расписание beat (tasks.py):
# `kg-signal-aggregates-compute` — hourly в :23, пишет window_end =
# floor(hour), а `kg-health-recompute` бежит */20 (:00/:20/:40). Нормальный
# возраст самой свежей записи гуляет от 37 мин до 1h23m: допуск в 1 час
# выкидывал агрегат в прогонах :00/:20 и пропускал в :40 — deploy_failure/
# slo_burn-штраф флапал каждые 20 минут, и что увидит дайджест/fragile-top
# было лотереей. Держим допуск в 3 периода записи: переживает один
# пропущенный hourly-прогон, а сама запись описывает окно window_hours=24,
# так что 3-часовая давность её не обесценивает.
_SIGNAL_AGG_WRITE_PERIOD_HOURS = 1
_SIGNAL_AGG_FRESHNESS_PERIODS = 3
_SIGNAL_AGG_FRESHNESS_HOURS = (
    _SIGNAL_AGG_WRITE_PERIOD_HOURS * _SIGNAL_AGG_FRESHNESS_PERIODS
)
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
    """Свежая (window_end >= now - _SIGNAL_AGG_FRESHNESS_HOURS) запись из
    kg_signal_aggregates.

    Допуск покрывает реальный интервал ЗАПИСИ агрегатов (hourly в :23), а не
    интервал этого пересчёта (*/20) — иначе штраф deploy_failure/slo_burn
    флапает внутри одного часа. См. _SIGNAL_AGG_FRESHNESS_HOURS.

    Если записи нет — None (вызывающий скипает deploy_failure / slo_burn).
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
    cutoff_alerts = now - timedelta(days=_OPEN_ALERT_MAX_AGE_DAYS)
    cutoff_events = now - timedelta(days=_POD_EVENT_LOOKBACK_DAYS)
    cutoff_recur = now - timedelta(hours=_RECURRENCE_WINDOW_HOURS)

    # Открытые alerts: «всё ещё открыт» = resolved_at IS NULL. Давность
    # fired_at НЕ ограничиваем 24 часами — иначе многодневный инцидент
    # (TTR p90 = 83h) перестаёт штрафовать именно тогда, когда он самый
    # болезненный. Отсекаем только фантомы старше 30д (см.
    # _OPEN_ALERT_MAX_AGE_DAYS).
    open_alerts = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.service_id == service.id,
            AlertEvent.resolved_at.is_(None),
            AlertEvent.fired_at >= cutoff_alerts,
        )
        .all()
    )
    open_critical = sum(1 for a in open_alerts if (a.severity or "").lower() == "critical")
    open_warning = sum(1 for a in open_alerts if (a.severity or "").lower() == "warning")

    # Chronic pod_events (BackOff/Unhealthy с count > 1000 — несколько суток
    # крашится; см. наш bot-service кейс с count=11789).
    # Окно считаем по last_seen, а НЕ по first_seen: k8s агрегирует повторы
    # в ОДНО событие с растущим count, поэтому у живого BackOff с count=11789
    # first_seen может быть недельной давности — фильтр по first_seen терял
    # ровно самые хронические кейсы. first_seen остаётся fallback'ом:
    # last_seen nullable (строки, записанные без dedup-апдейта).
    chronic_pod_events = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == service.id,
            PodEvent.count > 1000,
            or_(
                PodEvent.last_seen >= cutoff_events,
                and_(
                    PodEvent.last_seen.is_(None),
                    PodEvent.first_seen >= cutoff_events,
                ),
            ),
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
    # baseline нужен только для p95 drift — без свежего p95 его не читаем.
    baseline: Optional[float] = None
    if p95_recent is not None:
        baseline = _baseline_p95(db, cast(int, service.id), now)

    # ingress-derived 5xx (живой источник nginx-ingress; per-service http_5xx
    # выше всегда 0 — app /metrics за JWT, WO-12483).
    ingress_5xx_rate, ingress_p95_ms = _recent_ingress_5xx_p95(
        db, cast(int, service.id), now
    )

    # deploy_failure_pct / slo_burn_pct — из свежей записи kg_signal_aggregates
    agg = _latest_signal_aggregate(db, cast(int, service.id), now)

    return _score_from_inputs(
        _HealthInputs(
            open_critical=open_critical,
            open_warning=open_warning,
            chronic_pod_events=chronic_pod_events,
            recurrence_24h=recurrence_24h,
            p95_recent=p95_recent,
            err_recent=err_recent,
            n_points=n_points,
            baseline_p95=baseline,
            ingress_5xx_rate=ingress_5xx_rate,
            ingress_p95_ms=ingress_p95_ms,
            has_agg=agg is not None,
            deploy_failure_pct=agg.deploy_failure_pct if agg is not None else None,
            slo_burn_pct=agg.slo_burn_pct if agg is not None else None,
        ),
        namespace=service.namespace, name=service.name,
    )


@dataclass
class _HealthInputs:
    """Всё, что формула health_score читает из базы по одному сервису.

    Общая точка двух путей сбора: построчного (`compute_health_for_service`,
    запросы на сервис) и пакетного (`compute_health_for_services`, запросы на
    пачку). Формула (`_score_from_inputs`) у них одна, поэтому результат
    совпадает бит-в-бит.
    """
    open_critical: int = 0
    open_warning: int = 0
    chronic_pod_events: int = 0
    recurrence_24h: int = 0
    p95_recent: Optional[float] = None
    err_recent: Optional[float] = None
    n_points: int = 0
    baseline_p95: Optional[float] = None
    ingress_5xx_rate: Optional[float] = None
    ingress_p95_ms: Optional[float] = None
    has_agg: bool = False
    deploy_failure_pct: Optional[float] = None
    slo_burn_pct: Optional[float] = None


def _score_from_inputs(
    inp: _HealthInputs,
    namespace: Any = None,
    name: Any = None,
) -> Tuple[float, Dict[str, Optional[float]]]:
    """Формула health_score по собранным сигналам — без обращений к базе."""
    open_critical = inp.open_critical
    open_warning = inp.open_warning
    chronic_pod_events = inp.chronic_pod_events
    recurrence_24h = inp.recurrence_24h
    p95_recent, err_recent = inp.p95_recent, inp.err_recent

    # p95 drift: текущий vs baseline, penalty при > +50%
    p95_drift_pct: Optional[float] = None
    p95_drift_penalty = 0.0
    if p95_recent is not None:
        baseline = inp.baseline_p95
        if baseline and baseline > 0:
            drift = (p95_recent - baseline) / baseline * 100.0
            p95_drift_pct = drift
            if drift > _P95_DRIFT_TRIGGER_PCT:
                over = drift - _P95_DRIFT_TRIGGER_PCT
                p95_drift_penalty = _capped(over * _P95_DRIFT_WEIGHT_PER_PCT)

    # 5xx: единицы — 5xx-запросов/сек (НЕ доля), порог 0.05 rps.
    # Источник пока всегда 0 (app /metrics за JWT, WO-12483) — порог выставлен
    # так, чтобы после раскатки скрейпа он не срабатывал на первой же ошибке.
    http_5xx_penalty = 0.0
    if err_recent is not None and err_recent > _HTTP_5XX_TRIGGER_RPS:
        over = err_recent - _HTTP_5XX_TRIGGER_RPS
        http_5xx_penalty = _capped(over * _HTTP_5XX_WEIGHT_PER_RPS)

    # ingress-derived 5xx. Отдельный компонент: меряет 5xx-rps НА ГРАНИЦЕ
    # сервиса. p95 — информационный (нет baseline'а для drift'а на
    # ingress-разрезе), штрафуем только 5xx.
    ingress_5xx_rate, ingress_p95_ms = inp.ingress_5xx_rate, inp.ingress_p95_ms
    ingress_5xx_penalty = 0.0
    if ingress_5xx_rate is not None and ingress_5xx_rate > _INGRESS_5XX_TRIGGER_RPS:
        over = ingress_5xx_rate - _INGRESS_5XX_TRIGGER_RPS
        ingress_5xx_penalty = _capped(over * _INGRESS_5XX_WEIGHT)

    deploy_failure_pct: Optional[float] = None
    slo_burn_pct: Optional[float] = None
    deploy_failure_penalty = 0.0
    slo_burn_penalty = 0.0
    if inp.has_agg:
        deploy_failure_pct = inp.deploy_failure_pct
        slo_burn_pct = inp.slo_burn_pct
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

    signals: Dict[str, Optional[float]] = {
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
            namespace, name, score,
            open_critical * _PENALTY_PER_OPEN_CRITICAL,
            open_warning * _PENALTY_PER_OPEN_WARNING,
            chronic_pod_events * _PENALTY_CHRONIC_POD_EVENT,
            (recurrence_24h // 5) * _PENALTY_RECURRENCE_PER_5,
            p95_drift_penalty, http_5xx_penalty,
            deploy_failure_penalty, slo_burn_penalty,
            inp.n_points, "yes" if inp.has_agg else "no",
        )

    return score, signals


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def compute_health_for_services(
    db: Session,
    services: Sequence[Tuple[int, Any, Any]],
    now: Optional[datetime] = None,
) -> Dict[int, Tuple[float, Dict[str, Optional[float]]]]:
    """Health сразу для пачки сервисов: {service_id: (score, signals)}.

    `services` — тройки (id, namespace, name); namespace и name нужны только
    DEBUG-логу. Сигналы собираются шестью запросами на пачку вместо шести-семи
    на сервис; формула — та же `_score_from_inputs`, что у
    `compute_health_for_service`, и результат совпадает бит-в-бит (тест
    `tests/kg/test_health_batch_equivalence.py`).

    Порядок строк внутри сервиса зафиксирован (ORDER BY ts): среднее по float
    от порядка суммирования зависит, а построчный путь получает строки в
    порядке индекса (service_id, ts).
    """
    now = now or datetime.utcnow()
    ids = [int(sid) for sid, _, _ in services]
    result: Dict[int, Tuple[float, Dict[str, Optional[float]]]] = {}
    if not ids:
        return result
    inputs: Dict[int, _HealthInputs] = {sid: _HealthInputs() for sid in ids}

    cutoff_alerts = now - timedelta(days=_OPEN_ALERT_MAX_AGE_DAYS)
    cutoff_events = now - timedelta(days=_POD_EVENT_LOOKBACK_DAYS)
    cutoff_recur = now - timedelta(hours=_RECURRENCE_WINDOW_HOURS)

    for sid, severity in (
        db.query(AlertEvent.service_id, AlertEvent.severity)
        .filter(
            AlertEvent.service_id.in_(ids),
            AlertEvent.resolved_at.is_(None),
            AlertEvent.fired_at >= cutoff_alerts,
        )
        .all()
    ):
        sev = (severity or "").lower()
        if sev == "critical":
            inputs[cast(int, sid)].open_critical += 1
        elif sev == "warning":
            inputs[cast(int, sid)].open_warning += 1

    for sid, n in (
        db.query(PodEvent.service_id, func.count(PodEvent.id))
        .filter(
            PodEvent.service_id.in_(ids),
            PodEvent.count > 1000,
            or_(
                PodEvent.last_seen >= cutoff_events,
                and_(
                    PodEvent.last_seen.is_(None),
                    PodEvent.first_seen >= cutoff_events,
                ),
            ),
        )
        .group_by(PodEvent.service_id)
        .all()
    ):
        inputs[cast(int, sid)].chronic_pod_events = int(n)

    for sid, n in (
        db.query(AlertEvent.service_id, func.count(AlertEvent.id))
        .filter(
            AlertEvent.service_id.in_(ids),
            AlertEvent.fired_at >= cutoff_recur,
        )
        .group_by(AlertEvent.service_id)
        .all()
    ):
        inputs[cast(int, sid)].recurrence_24h = int(n)

    # Последний час метрик: средний p95 и 5xx, если точек достаточно.
    recent: Dict[int, List[Tuple[Any, Any]]] = {}
    for sid, p95, err in (
        db.query(
            ServiceHealth.service_id,
            ServiceHealth.p95_latency_ms,
            ServiceHealth.http_5xx_rate,
        )
        .filter(
            ServiceHealth.service_id.in_(ids),
            ServiceHealth.ts >= now - timedelta(hours=_METRIC_RECENT_WINDOW_HOURS),
        )
        .order_by(ServiceHealth.service_id, ServiceHealth.ts)
        .all()
    ):
        recent.setdefault(cast(int, sid), []).append((p95, err))
    for sid, points in recent.items():
        inp = inputs[sid]
        inp.n_points = len(points)
        if len(points) < _MIN_POINTS_FOR_METRICS:
            continue
        inp.p95_recent = _mean([float(p) for p, _ in points if p is not None])
        inp.err_recent = _mean([float(e) for _, e in points if e is not None])

    # Baseline p95 за 7 дней — только тем, у кого есть свежий p95. Пока app
    # /metrics закрыт (WO-12483), таких нет и запрос не выполняется вовсе.
    need_baseline = [sid for sid in ids if inputs[sid].p95_recent is not None]
    if need_baseline:
        baseline_vals: Dict[int, List[float]] = {}
        for sid, p95 in (
            db.query(ServiceHealth.service_id, ServiceHealth.p95_latency_ms)
            .filter(
                ServiceHealth.service_id.in_(need_baseline),
                ServiceHealth.ts >= now - timedelta(days=_METRIC_BASELINE_WINDOW_DAYS),
                ServiceHealth.ts < now - timedelta(hours=_METRIC_RECENT_WINDOW_HOURS),
                ServiceHealth.p95_latency_ms.isnot(None),
            )
            .order_by(ServiceHealth.service_id, ServiceHealth.ts)
            .all()
        ):
            baseline_vals.setdefault(cast(int, sid), []).append(float(cast(float, p95)))
        for sid, vals in baseline_vals.items():
            inputs[sid].baseline_p95 = _mean(vals)

    # Ingress: последняя запись на endpoint (host, path) в окне, потом максимум.
    latest_obs: Dict[int, Dict[Tuple[Any, Any], Tuple[Any, Any]]] = {}
    for sid, host, path, err, p95 in (
        db.query(
            IngressObservation.service_id,
            IngressObservation.host,
            IngressObservation.path,
            IngressObservation.error_5xx_rate,
            IngressObservation.p95_latency_ms,
        )
        .filter(
            IngressObservation.service_id.in_(ids),
            IngressObservation.ts
            >= now - timedelta(minutes=_INGRESS_RECENT_WINDOW_MINUTES),
        )
        .order_by(IngressObservation.ts.desc())
        .all()
    ):
        latest_obs.setdefault(cast(int, sid), {}).setdefault((host, path), (err, p95))
    for sid, by_endpoint in latest_obs.items():
        err_vals = [float(e) for e, _ in by_endpoint.values() if e is not None]
        p95_vals = [float(p) for _, p in by_endpoint.values() if p is not None]
        inputs[sid].ingress_5xx_rate = max(err_vals) if err_vals else None
        inputs[sid].ingress_p95_ms = max(p95_vals) if p95_vals else None

    # Свежая запись kg_signal_aggregates: первая по убыванию window_end.
    for sid, deploy_failure_pct, slo_burn_pct in (
        db.query(
            SignalAggregate.service_id,
            SignalAggregate.deploy_failure_pct,
            SignalAggregate.slo_burn_pct,
        )
        .filter(
            SignalAggregate.service_id.in_(ids),
            SignalAggregate.window_end
            >= now - timedelta(hours=_SIGNAL_AGG_FRESHNESS_HOURS),
        )
        .order_by(SignalAggregate.window_end.desc())
        .all()
    ):
        inp = inputs[cast(int, sid)]
        if inp.has_agg:
            continue
        inp.has_agg = True
        inp.deploy_failure_pct = deploy_failure_pct
        inp.slo_burn_pct = slo_burn_pct

    for sid, namespace, name in services:
        result[int(sid)] = _score_from_inputs(
            inputs[int(sid)], namespace=namespace, name=name,
        )
    return result


def recompute_all_health(db: Session) -> Dict[str, int]:
    """Beat task entry — пересчитать health_score для ВСЕХ real services.
    Synthetic пропускаются (по дизайну нет meaningful health).

    Returns stats: {real_services, recomputed, low_health, perfect_health}.
    """
    now = datetime.utcnow()
    # ORDER BY id — детерминированный состав пачек: два встречных прохода
    # режут сервисы на одни и те же сотни, и пакетный UPDATE каждой берёт
    # блокировки одним планом в одном порядке — схлопнуться в deadlock между
    # собой проходы не могут.
    # node_kind='service': с contract 2.4 у пары «Service foo + Deployment foo»
    # два non-synthetic узла. Без фильтра пересчёт делал двойную работу
    # (~9k UPDATE вместо ~4.5k — ровно те row-локи, что кормили deadlock'и
    # 09-10.08) и давал два неразличимых ряда в top_unhealthy.
    # Лёгкие строки (id, namespace, name), а не ORM-объекты: пересчёт пишет
    # пакетным UPDATE, и тянуть ради него metadata_json 6,6 тыс. узлов незачем.
    services: List[Tuple[int, str, str]] = [
        (int(sid), str(ns), str(name)) for sid, ns, name in (
            db.query(Service.id, Service.namespace, Service.name)
            .filter(
                Service.synthetic.is_(False),
                Service.node_kind == NODE_KIND_SERVICE,
            )
            .order_by(Service.id)
            .all()
        )
    ]
    # Узлы namespace, которого нет в кластере, не пересчитываем: свежий
    # health_score у снесённого стенда читался как признак жизни (squad-42,
    # 08.09.2026 — health_computed_at «сегодня» при namespace, удалённом вчера).
    gone_ns = missing_namespace_names(db)
    skipped_missing_ns = 0
    if gone_ns:
        kept = [s for s in services if s[1] not in gone_ns]
        skipped_missing_ns = len(services) - len(kept)
        services = kept
    low_health = 0
    perfect_health = 0
    recomputed = 0
    # Commit батчами. При autoflush=False единственный db.commit() в конце
    # флашил ВСЕ ~9k UPDATE-ов одной транзакцией — десятки секунд накопления
    # row-локов на kg_services, встречные upsert-ы синка топологии ловили
    # deadlock (594/сутки на проде 09-10.08). Короткие транзакции отпускают
    # локи каждые ~100 строк; пересчёт идемпотентен, частичный прогон
    # безопасен.
    #
    # С 18.11.2026 пачка — это ещё и единица чтения и записи: сигналы
    # собираются шестью запросами на пачку (`compute_health_for_services`), а
    # не шестью на сервис, и пишутся одним UPDATE на пачку вместо UPDATE на
    # строку. Было ~8 запросов на сервис (8,5 млн UPDATE за 18 дней), стало
    # 7 на сотню.
    # Локи на kg_services берутся на время одного UPDATE и отпускаются
    # коммитом сразу за ним — не дольше, чем раньше.
    _COMMIT_BATCH = 100
    for start in range(0, len(services), _COMMIT_BATCH):
        batch = services[start:start + _COMMIT_BATCH]
        computed = compute_health_for_services(db, batch, now=now)
        score_by_id: Dict[int, float] = {}
        for sid, _, _ in batch:
            score = computed[sid][0]
            score_by_id[sid] = score
            recomputed += 1
            if score < 0.4:
                low_health += 1
            if score >= 0.99:
                perfect_health += 1
        # Один UPDATE на пачку: score — CASE по id. updated_at двигает
        # onupdate колонки, как и при ORM-записи.
        db.query(Service).filter(Service.id.in_(list(score_by_id))).update(
            {
                Service.health_score: case(score_by_id, value=Service.id),
                Service.health_computed_at: now,
            },
            synchronize_session=False,
        )
        db.commit()
    log.info(
        "kg_health.recompute_done real=%d low_health=%d perfect=%d",
        len(services), low_health, perfect_health,
    )
    return {
        "real_services": len(services),
        "skipped_missing_ns": skipped_missing_ns,
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
        # node_kind: recompute_all_health больше не пишет score workload-узлам,
        # но у legacy-строк (пересчитанных до фикса) health_score уже выставлен
        # и никогда не чистится — без фильтра пара «Service + workload»
        # давала бы два неразличимых ряда в топе.
        Service.node_kind == NODE_KIND_SERVICE,
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
