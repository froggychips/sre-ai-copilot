"""Deploy correlator — связь между alert/incident и недавним deploy сервиса.

При срабатывании alert ищем ближайший по времени deploy того же сервиса
(в окне lookback_hours до инцидента) и сравниваем «до vs после» по метрикам
из kg_service_health. На основе нескольких факторов (N_spikes, max |z-score|,
time-proximity, flat-baseline penalty, deploy-status weight) считаем
confidence в [0..1] и выдаём verdict-градацию:

    confidence >= 0.7        → likely     (сильный сигнал)
    0.4 <= conf < 0.7        → suspect    (заметный сигнал)
    0.2 <= conf < 0.4        → weak       (слабый сигнал)
    conf < 0.2               → unlikely   (фоновое движение)

Используется RCA-пайплайном как дополнительный сигнал «релиз сломал прод».
Read-only: только select-ы, никаких commit-ов.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import Deployment, ServiceHealth

# Метрики, по которым сравниваем before/after. Имена должны совпадать с
# колонками kg_service_health.
_METRICS = (
    "cpu_pct",
    "mem_pct",
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
)

# Порог, при превышении которого считаем метрику «всплеском» в N_spikes.
_SUSPECT_DELTA_PCT = 50.0

# Если before_avg близок к нулю — относительная дельта не имеет смысла.
# Считаем «всплеск» только если абсолютное значение after выше этого минимума.
_NEAR_ZERO_EPS = 1e-6
_ABS_SPIKE_MIN = {
    "cpu_pct": 5.0,           # 0% → 5% cpu — это реально всплеск
    "mem_pct": 5.0,
    "restarts_rate": 0.01,     # 1 рестарт / 100s
    "http_5xx_rate": 0.1,      # 0.1 rps 5xx
    "p95_latency_ms": 50.0,    # 50ms
}

# Verdict-пороги по confidence. Если хочется поправить тарировку —
# трогать здесь, не в коде.
_VERDICT_LIKELY = 0.7
_VERDICT_SUSPECT = 0.4
_VERDICT_WEAK = 0.2

# Time-proximity decay: confidence × exp(-Δt_min / TAU). TAU=30min даёт
# 0 мин → 1.0, 60 мин → ~0.14, 120 мин → ~0.02.
_TIME_DECAY_TAU_MIN = 30.0

# Если у всех метрик baseline-stddev ≈ 0 (flat-line, low-traffic dev-сервис),
# z-score не информативен — рубим confidence до 30% от исходной.
_FLAT_BASELINE_PENALTY = 0.3
_FLAT_BASELINE_STDDEV_EPS = 1e-6

# Deploy-status weighting: FAILURE deploy более вероятный suspect.
_STATUS_FACTOR_FAILURE = 1.2
_STATUS_FACTOR_SUCCESS = 1.0


def _strip_tz(dt: datetime) -> datetime:
    """kg_service_health.ts хранится как naive UTC — приводим к тому же виду."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _values(rows, attr: str) -> List[float]:
    """Список non-NULL числовых значений по атрибуту."""
    out: List[float] = []
    for r in rows:
        v = getattr(r, attr, None)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _avg(rows, attr: str) -> Optional[float]:
    """Среднее по атрибуту, пропуская None. None если нет данных."""
    values = _values(rows, attr)
    if not values:
        return None
    return sum(values) / len(values)


def _delta_pct(before: Optional[float], after: Optional[float],
               metric: str) -> Optional[float]:
    """Относительное изменение в процентах с защитой от before≈0."""
    if before is None or after is None:
        return None
    if abs(before) < _NEAR_ZERO_EPS:
        # before почти 0 — относительная дельта не определена.
        # Сигналим всплеск только если after сам по себе выше порога.
        min_abs = _ABS_SPIKE_MIN.get(metric, 0.0)
        if after >= min_abs:
            # Условно «бесконечный» рост — отдаём явный большой процент,
            # чтобы verdict сработал. 9999 — маркер «спайк с нуля».
            return 9999.0
        return 0.0
    return (after - before) / abs(before) * 100.0


def _baseline_stddev(values: List[float]) -> Optional[float]:
    """Population stddev по списку. None если <2 точек."""
    if len(values) < 2:
        return None
    try:
        return statistics.pstdev(values)
    except statistics.StatisticsError:
        return None


def _metric_zscore(
    before_vals: List[float], after_avg: Optional[float],
) -> Optional[float]:
    """|z| текущего after_avg относительно before-распределения.

    Возвращает None если baseline слишком мал/плоский для оценки.
    """
    if after_avg is None or len(before_vals) < 2:
        return None
    mean = statistics.fmean(before_vals)
    stddev = _baseline_stddev(before_vals)
    if stddev is None or stddev < _FLAT_BASELINE_STDDEV_EPS:
        return None
    return abs((after_avg - mean) / stddev)


def _verdict_for(confidence: float) -> str:
    """Маппинг confidence → verdict-тег."""
    if confidence >= _VERDICT_LIKELY:
        return "likely"
    if confidence >= _VERDICT_SUSPECT:
        return "suspect"
    if confidence >= _VERDICT_WEAK:
        return "weak"
    return "unlikely"


def correlate_deploy_to_incident(
    db: Session,
    service_id: int,
    incident_ts: datetime,
    lookback_hours: int = 2,
    metric_window_minutes: int = 60,
) -> Dict[str, Any]:
    """Поиск ближайшего deploy и сравнение метрик до/после.

    Args:
        db: SQLAlchemy Session (sync) — read-only.
        service_id: kg_services.id.
        incident_ts: момент, на который выходит alert/incident.
        lookback_hours: насколько далеко назад от incident_ts ищем deploy.
        metric_window_minutes: окно усреднения метрик до и после deploy.

    Returns:
        Если deploy найден:
            {
              "deploy": {id, started_at, sha, buildtype_id, status, ...},
              "metrics_diff": {
                "cpu_pct": {"before": .., "after": .., "delta_pct": ..,
                            "zscore": ..},
                ...
              },
              "verdict": "likely" | "suspect" | "weak" | "unlikely",
              "confidence": 0..1,
              "n_spikes": int,
              "max_zscore": float | None,
              "time_proximity_minutes": int,
              "scoring": { factor breakdown for debug },
            }
        Иначе: {"deploy": None, "reason": "no_recent_deploy"}.
    """
    incident_naive = _strip_tz(incident_ts)
    lookback_start = incident_naive - timedelta(hours=lookback_hours)

    deploy: Optional[Deployment] = (
        db.query(Deployment)
        .filter(
            Deployment.service_id == service_id,
            Deployment.started_at >= lookback_start,
            Deployment.started_at <= incident_naive,
        )
        .order_by(Deployment.started_at.desc())
        .first()
    )

    if deploy is None:
        return {"deploy": None, "reason": "no_recent_deploy"}

    deploy_ts = deploy.started_at  # naive UTC (как и пишет beat-task)
    before_start = deploy_ts - timedelta(minutes=metric_window_minutes)
    after_end = deploy_ts + timedelta(minutes=metric_window_minutes)

    # Берём метрики в обоих окнах одним запросом — индекс
    # ix_kg_service_health_service_ts покрывает (service_id, ts).
    rows = (
        db.query(ServiceHealth)
        .filter(
            ServiceHealth.service_id == service_id,
            ServiceHealth.ts >= before_start,
            ServiceHealth.ts <= after_end,
        )
        .all()
    )

    before_rows = [r for r in rows if r.ts <= deploy_ts]
    after_rows = [r for r in rows if r.ts > deploy_ts]

    # --- Per-metric statistics --------------------------------------------
    metrics_diff: Dict[str, Dict[str, Optional[float]]] = {}
    n_spikes = 0
    z_scores: List[float] = []
    stddev_present_count = 0  # сколько метрик дали валидный stddev для z

    for metric in _METRICS:
        before_vals = _values(before_rows, metric)
        after_avg = _avg(after_rows, metric)
        before_avg = sum(before_vals) / len(before_vals) if before_vals else None
        delta = _delta_pct(before_avg, after_avg, metric)
        z = _metric_zscore(before_vals, after_avg)

        metrics_diff[metric] = {
            "before": before_avg,
            "after": after_avg,
            "delta_pct": delta,
            "zscore": z,
        }

        if delta is not None and delta > _SUSPECT_DELTA_PCT:
            n_spikes += 1
        if z is not None:
            z_scores.append(z)
            stddev_present_count += 1

    max_zscore = max(z_scores) if z_scores else None

    # --- Time-proximity decay ---------------------------------------------
    delta_minutes = (incident_naive - deploy_ts).total_seconds() / 60.0
    # Клипуем отрицательное (deploy после incident — теоретически невозможно
    # по фильтру, но на всякий случай) и не-числа.
    delta_minutes = max(0.0, delta_minutes)
    time_proximity = math.exp(-delta_minutes / _TIME_DECAY_TAU_MIN)

    # --- Deploy-status factor ---------------------------------------------
    status = (deploy.status or "").upper()
    if status == "FAILURE":
        status_factor = _STATUS_FACTOR_FAILURE
    else:
        status_factor = _STATUS_FACTOR_SUCCESS

    # --- Flat-baseline penalty --------------------------------------------
    # Если ни одна метрика не дала валидный stddev — z-score вообще
    # неинформативен; в этой ситуации сигнал по spike-проценту можно
    # принять, но с понижающим коэффициентом.
    flat_penalty = (
        _FLAT_BASELINE_PENALTY if stddev_present_count == 0 else 1.0
    )

    # --- Final confidence -------------------------------------------------
    n_spike_factor = n_spikes / float(len(_METRICS))  # 0..1
    z_factor = min((max_zscore or 0.0) / 5.0, 1.0)
    # 50/50 вес между «сколько метрик спайкнули» и «насколько сильно худшая».
    spike_component = 0.5 * n_spike_factor + 0.5 * z_factor

    raw = time_proximity * status_factor * spike_component
    confidence = min(1.0, max(0.0, raw)) * flat_penalty
    # Финальный clip — flat_penalty может только ронять, не растить.
    confidence = max(0.0, min(1.0, confidence))

    verdict = _verdict_for(confidence)

    extras: Dict[str, Any] = deploy.extras if isinstance(deploy.extras, dict) else {}
    deploy_dict: Dict[str, Any] = {
        "id": deploy.id,
        "service_id": deploy.service_id,
        "sha": deploy.sha,
        "repo": deploy.repo,
        "buildtype_id": deploy.buildtype_id,
        "build_number": deploy.build_number,
        "started_at": deploy.started_at.isoformat() if deploy.started_at else None,
        "finished_at": deploy.finished_at.isoformat() if deploy.finished_at else None,
        "status": deploy.status,
        "triggered_by": deploy.triggered_by,
        "url": extras.get("url"),
        "minutes_before_incident": int(delta_minutes),
    }

    return {
        "deploy": deploy_dict,
        "metrics_diff": metrics_diff,
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "n_spikes": n_spikes,
        "max_zscore": (round(max_zscore, 4) if max_zscore is not None else None),
        "time_proximity_minutes": int(delta_minutes),
        "scoring": {
            "time_proximity": round(time_proximity, 4),
            "status_factor": status_factor,
            "n_spike_factor": round(n_spike_factor, 4),
            "z_factor": round(z_factor, 4),
            "spike_component": round(spike_component, 4),
            "flat_baseline_penalty": flat_penalty,
        },
    }
