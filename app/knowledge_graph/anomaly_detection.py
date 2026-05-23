"""Robust-z anomaly detection по kg_service_health.

Beat-task `kg_anomaly_detection_task` каждые ~10 мин:
1. Берём все real (synthetic=False) services из KG.
2. Для каждого сервиса:
   - current_window = последние 6 точек (≈1ч при 10-мин ритме) → текущее
     значение метрики = mean этих точек (устойчивее к одной выбросной точке).
   - baseline_window = 7d, исключая последний 1ч (чтобы текущий всплеск не
     подмешивался в baseline).
   - robust_z = (current - median(baseline)) / (1.4826 × MAD(baseline)).
     MAD = median absolute deviation. 1.4826 — gauss-consistency-constant.
     В отличие от mean/stddev устойчиво к outlier-ам в baseline
     (deploy spike предыдущего дня не «отравит» порог).
3. **Опциональный seasonal baseline**: если baseline ≥ 50 точек, бьём
   точки по hour-of-day и сравниваем current с baseline-точками текущего
   часа ±1. Иначе fallback на плоский baseline. Снижает false-positive
   shower при day/night паттернах.
4. Защиты от ложных срабатываний:
   - baseline_n < 10 → пропускаем метрику (мало данных).
   - MAD < 1e-6 → пропускаем (flat-line после удаления outlier-ов).
   - NULL в value → пропускаем эту метрику для этой точки.
5. **Volume guard**: per-service per-metric не более 3 anomaly
   observations за последний час. После 3-го — игнорируем (защита от
   «постоянно деградирующего сервиса» flood-а в digest).
6. Пороги: |robust_z| > KG_ANOMALY_ROBUST_Z_WARN → warning,
            |robust_z| > KG_ANOMALY_ROBUST_Z_CRIT → critical.
   Default 3.5 / 6.0. Trigger >=, не строго >.
7. Идемпотентность: UNIQUE(service_id, ts, metric) + per-row savepoint.

Discord-уведомление в этой фазе не реализуется — пишем notified=false,
вторая фаза обработает unsent.

CLI: `python -m app.knowledge_graph.anomaly_detection`.
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (AnomalyObservation, Service,
                                        ServiceHealth)

log = logging.getLogger(__name__)

# Метрики, по которым считаем robust-z. Должны существовать в ServiceHealth.
METRICS: Tuple[str, ...] = (
    "cpu_pct",
    "mem_pct",
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
)

# Параметры окон.
CURRENT_POINTS = 6              # последние ≈1ч при 10-мин ритме
BASELINE_DAYS = 7               # окно baseline
BASELINE_EXCLUDE_HOURS = 1      # исключить последний час из baseline
MIN_BASELINE_POINTS = 10        # ниже — слишком мало, пропускаем

# MAD ниже этого считаем «плоской линией» — z не информативен.
MIN_MAD = 1e-6

# 1.4826 — gauss-consistency-constant: 1 / Φ⁻¹(0.75). При нормальном
# распределении даёт MAD ≈ stddev, поэтому robust_z сопоставим со стандартным.
MAD_GAUSS_CONST = 1.4826

# Seasonal baseline активируется только при достаточном объёме.
SEASONAL_MIN_POINTS = 50
SEASONAL_HOUR_BAND = 1          # ±1 час от target hour-of-day

# Volume guard: per-service per-metric не более N observations за окно.
VOLUME_GUARD_MAX_PER_HOUR = 3
VOLUME_GUARD_WINDOW = timedelta(hours=1)


def _threshold_warn() -> float:
    """Порог warning из env с safe-fallback."""
    raw = os.environ.get("KG_ANOMALY_ROBUST_Z_WARN", "3.5")
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("anomaly.bad_warn_threshold raw=%r → using 3.5", raw)
        return 3.5


def _threshold_crit() -> float:
    """Порог critical из env с safe-fallback."""
    raw = os.environ.get("KG_ANOMALY_ROBUST_Z_CRIT", "6.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("anomaly.bad_crit_threshold raw=%r → using 6.0", raw)
        return 6.0


def _extract_values(
    rows: List[ServiceHealth], metric: str,
) -> List[float]:
    """Из списка health-rows достаёт non-NULL значения по метрике."""
    out: List[float] = []
    for r in rows:
        v = getattr(r, metric, None)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _mad(values: List[float]) -> Optional[float]:
    """Median Absolute Deviation. None если <2 точек."""
    if len(values) < 2:
        return None
    med = statistics.median(values)
    abs_devs = [abs(v - med) for v in values]
    return statistics.median(abs_devs)


def _compute_robust_z(
    current: float, baseline: List[float],
) -> Optional[Tuple[float, float, float]]:
    """Возвращает (robust_z, median, mad) или None если baseline недостаточен.

    robust_z = (current - median) / (1.4826 × MAD).
    """
    if len(baseline) < MIN_BASELINE_POINTS:
        return None
    try:
        med = statistics.median(baseline)
    except statistics.StatisticsError:
        return None
    mad = _mad(baseline)
    if mad is None or mad < MIN_MAD:
        return None
    scaled_mad = MAD_GAUSS_CONST * mad
    if scaled_mad < MIN_MAD:
        return None
    z = (current - med) / scaled_mad
    return z, med, mad


def _seasonal_baseline(
    rows: List[ServiceHealth], metric: str, target_hour: int,
) -> List[float]:
    """Отфильтровать baseline-точки по hour-of-day ±SEASONAL_HOUR_BAND.

    Например target_hour=14 → берём только точки с hour ∈ {13, 14, 15}.
    Учитываем wrap-around через сутки: hour 23 → {22, 23, 0}.
    """
    band = SEASONAL_HOUR_BAND
    allowed = {(target_hour + i) % 24 for i in range(-band, band + 1)}
    out: List[float] = []
    for r in rows:
        if r.ts is None or r.ts.hour not in allowed:
            continue
        v = getattr(r, metric, None)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _severity(z: float, warn: float, crit: float) -> str:
    return "critical" if abs(z) >= crit else "warning"


def _count_recent_observations(
    db: Session, service_id: int, metric: str, now: datetime,
) -> int:
    """Сколько уже было записано аномалий за последний час по
    (service_id, metric). Volume guard.

    Окно считаем по `ts` (анамалии-таймстамп), не по `created_at`. Это
    даёт детерминированное окно в тестах (где `now` подменяется) и
    в обычном проде работает идентично (kg_service_health.ts ≈ now).
    """
    cutoff = now - VOLUME_GUARD_WINDOW
    return (
        db.query(AnomalyObservation)
        .filter(
            AnomalyObservation.service_id == service_id,
            AnomalyObservation.metric == metric,
            AnomalyObservation.ts >= cutoff,
        )
        .count()
    )


def _insert_idempotent(
    db: Session,
    *,
    service_id: int,
    ts: datetime,
    metric: str,
    value: float,
    baseline_mean: float,
    baseline_stddev: float,
    z_score: float,
    severity: str,
    extras: Optional[Dict[str, Any]] = None,
) -> bool:
    """INSERT с защитой по UNIQUE(service_id, ts, metric)."""
    row = AnomalyObservation(
        service_id=service_id,
        ts=ts,
        metric=metric,
        value=value,
        baseline_mean=baseline_mean,
        baseline_stddev=baseline_stddev,
        z_score=z_score,
        severity=severity,
        notified=False,
        extras=extras,
    )
    try:
        with db.begin_nested():
            db.add(row)
        return True
    except IntegrityError:
        return False


def _detect_for_service(
    db: Session,
    service: Service,
    now: datetime,
    *,
    warn_thresh: float,
    crit_thresh: float,
) -> Dict[str, int]:
    """Считает аномалии для одного сервиса.

    Возвращает счётчики: inserted / skipped_dup / skipped_no_baseline /
    skipped_no_current / skipped_volume_guard.
    """
    counters = {
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_no_baseline": 0,
        "skipped_no_current": 0,
        "skipped_volume_guard": 0,
    }

    current_start = now - timedelta(hours=BASELINE_EXCLUDE_HOURS)
    baseline_start = now - timedelta(days=BASELINE_DAYS)

    rows: List[ServiceHealth] = (
        db.query(ServiceHealth)
        .filter(
            ServiceHealth.service_id == service.id,
            ServiceHealth.ts >= baseline_start,
            ServiceHealth.ts <= now,
        )
        .order_by(ServiceHealth.ts.asc())
        .all()
    )
    if not rows:
        return counters

    baseline_rows = [r for r in rows if r.ts < current_start]
    current_rows = [r for r in rows if r.ts >= current_start][-CURRENT_POINTS:]
    if not current_rows:
        counters["skipped_no_current"] += len(METRICS)
        return counters

    anomaly_ts = current_rows[-1].ts
    target_hour = anomaly_ts.hour

    for metric in METRICS:
        current_vals = _extract_values(current_rows, metric)
        if not current_vals:
            counters["skipped_no_current"] += 1
            continue
        current_value = statistics.fmean(current_vals)

        # Сначала пробуем seasonal baseline если данных достаточно.
        baseline_all = _extract_values(baseline_rows, metric)
        method = "robust_z_flat"
        baseline_vals = baseline_all

        if len(baseline_all) >= SEASONAL_MIN_POINTS:
            seasonal = _seasonal_baseline(baseline_rows, metric, target_hour)
            # Seasonal-окно тоже должно набрать MIN_BASELINE_POINTS — иначе
            # fallback на flat.
            if len(seasonal) >= MIN_BASELINE_POINTS:
                baseline_vals = seasonal
                method = "robust_z_seasonal"

        res = _compute_robust_z(current_value, baseline_vals)
        if res is None:
            counters["skipped_no_baseline"] += 1
            continue
        z, median, mad = res
        if abs(z) < warn_thresh:
            continue

        # Volume guard: не плодим observations того же service/metric.
        recent_count = _count_recent_observations(
            db, service.id, metric, now,
        )
        if recent_count >= VOLUME_GUARD_MAX_PER_HOUR:
            counters["skipped_volume_guard"] += 1
            continue

        sev = _severity(z, warn_thresh, crit_thresh)
        extras = {
            "method": method,
            "median": median,
            "mad": mad,
            "baseline_n": len(baseline_vals),
            "warn_thresh": warn_thresh,
            "crit_thresh": crit_thresh,
            "target_hour": target_hour,
        }

        # baseline_mean/baseline_stddev по схеме — пишем median и
        # scaled_mad (MAD × 1.4826). Это сохраняет смысл колонок
        # (центр + разброс) и читаемо для downstream-сервисов.
        ok = _insert_idempotent(
            db,
            service_id=service.id,
            ts=anomaly_ts,
            metric=metric,
            value=current_value,
            baseline_mean=median,
            baseline_stddev=MAD_GAUSS_CONST * mad,
            z_score=z,
            severity=sev,
            extras=extras,
        )
        if ok:
            counters["inserted"] += 1
        else:
            counters["skipped_dup"] += 1

    return counters


def detect_anomalies(
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Beat-task entry — детектировать аномалии по всем real services.

    `now` — для тестов (фиксированное время). Default = datetime.utcnow().
    """
    now = now or datetime.utcnow()
    warn_thresh = _threshold_warn()
    crit_thresh = _threshold_crit()

    services: List[Service] = (
        db.query(Service).filter(Service.synthetic.is_(False)).all()
    )
    stats: Dict[str, Any] = {
        "real_services": len(services),
        "now": now.isoformat(),
        "warn_thresh": warn_thresh,
        "crit_thresh": crit_thresh,
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_no_baseline": 0,
        "skipped_no_current": 0,
        "skipped_volume_guard": 0,
        "errors": 0,
        "by_severity": {"warning": 0, "critical": 0},
    }

    for svc in services:
        try:
            counters = _detect_for_service(
                db, svc, now,
                warn_thresh=warn_thresh, crit_thresh=crit_thresh,
            )
        except Exception as e:
            stats["errors"] += 1
            log.warning(
                "anomaly_detection.compute_failed ns=%s name=%s err=%s",
                svc.namespace, svc.name, e,
            )
            continue
        stats["inserted"] += counters["inserted"]
        stats["skipped_dup"] += counters["skipped_dup"]
        stats["skipped_no_baseline"] += counters["skipped_no_baseline"]
        stats["skipped_no_current"] += counters["skipped_no_current"]
        stats["skipped_volume_guard"] += counters["skipped_volume_guard"]

    db.commit()

    # Подсчёт by_severity по только что вставленным.
    try:
        recent_cutoff = now - timedelta(minutes=1)
        sev_rows = (
            db.query(AnomalyObservation.severity)
            .filter(AnomalyObservation.created_at >= recent_cutoff)
            .all()
        )
        for (sev,) in sev_rows:
            if sev in stats["by_severity"]:
                stats["by_severity"][sev] += 1
    except Exception:
        pass

    log.info(
        "anomaly_detection.done real=%d inserted=%d (warn=%d crit=%d) "
        "skipped_dup=%d skipped_no_baseline=%d skipped_no_current=%d "
        "skipped_volume_guard=%d errors=%d thresh=%.1f/%.1f",
        stats["real_services"], stats["inserted"],
        stats["by_severity"]["warning"], stats["by_severity"]["critical"],
        stats["skipped_dup"], stats["skipped_no_baseline"],
        stats["skipped_no_current"], stats["skipped_volume_guard"],
        stats["errors"], warn_thresh, crit_thresh,
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(detect_anomalies(db))
    finally:
        db.close()
