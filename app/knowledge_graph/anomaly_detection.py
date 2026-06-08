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
   - Noise floor: знаменатель z = max(scaled_mad, rel_floor×|median|,
     abs_floor[metric]). Near-flat baseline (MAD≈0) больше не даёт z=900 на
     сдвиге в пару п.п.; severity не инфлируется. rel_floor — env
     KG_ANOMALY_REL_SPREAD_FLOOR (default 0.10).
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
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (AnomalyObservation, LogObservation,
                                        Service, ServiceHealth)

log = logging.getLogger(__name__)

# Метрики, по которым считаем robust-z. Должны существовать в ServiceHealth.
METRICS: Tuple[str, ...] = (
    "cpu_pct",
    "mem_pct",
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
)

# Лог-производный app-сигнал (consumer для log_error_rate, см. queries.py).
# Отдельная метрика поверх kg_log_observations — НЕ из ServiceHealth, т.к.
# http_5xx/p95 там всегда 0 (scrape-gap), а лог-ошибки реально есть.
# Семантика: всплеск Error/Fatal-логов сервиса относительно его ЖЕ типичного
# объёма ошибок (НЕ HTTP 5xx). Это proxy — помечаем в extras.
LOG_ERROR_METRIC = "log_error_rate"
LOG_ERROR_LEVELS: Tuple[str, ...] = ("Error", "Fatal")

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

# ── Noise floor (WO-11335/KG-polish) ──────────────────────────────────────
# Без пола near-flat baseline (MAD≈0) раздувает robust-z на ничтожном
# АБСОЛЮТНОМ сдвиге: наблюдали z=916 при mem 6.87→8.04% (1.2 п.п.) и z=−679
# при mem 61.8→59.5% (2.3 п.п.) — формально critical, практически шум.
# Решение: эффективный разброс в знаменателе z не может быть меньше
#   max(scaled_mad, REL_floor × |median|, ABS_floor[metric]).
# Тогда z остаётся осмысленным (мелкие относительные колебания дают малый z),
# а severity (warn/crit) перестаёт инфлироваться. Бонус: сервис, годами
# стоявший на месте (MAD=0), при реальном крупном скачке теперь детектируется
# (раньше MAD<MIN_MAD его молча пропускал).
def _rel_spread_floor() -> float:
    """Относительный пол разброса (доля |median|). Env-tunable без редеплоя."""
    raw = os.environ.get("KG_ANOMALY_REL_SPREAD_FLOOR", "0.10")
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("anomaly.bad_rel_spread_floor raw=%r → using 0.10", raw)
        return 0.10


# Абсолютный пол разброса в нативных единицах метрики — для случаев, когда
# median≈0 и относительный пол вырождается в ноль.
MIN_ABS_SPREAD_BY_METRIC: Dict[str, float] = {
    "cpu_pct": 0.02,
    "mem_pct": 1.0,
    "restarts_rate": 0.05,
    "http_5xx_rate": 0.1,
    "p95_latency_ms": 5.0,
    LOG_ERROR_METRIC: 1.0,
}
DEFAULT_MIN_ABS_SPREAD = 1e-3

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
    current: float,
    baseline: List[float],
    *,
    rel_floor: float = 0.0,
    abs_floor: float = 0.0,
) -> Optional[Tuple[float, float, float, float]]:
    """Возвращает (robust_z, median, effective_spread, raw_mad) или None.

    robust_z = (current - median) / effective_spread, где
      effective_spread = max(1.4826 × MAD, rel_floor × |median|, abs_floor).

    Пол разброса (rel_floor/abs_floor) гасит z-взрыв на near-flat baseline:
    без него крошечный MAD делал любой микро-сдвиг «critical» (см. noise floor
    в шапке модуля). При rel_floor=abs_floor=0 поведение — как раньше (чистый MAD),
    но тогда MAD≈0 даёт вырожденный спред → None.
    """
    if len(baseline) < MIN_BASELINE_POINTS:
        return None
    try:
        med = statistics.median(baseline)
    except statistics.StatisticsError:
        return None
    mad = _mad(baseline)
    if mad is None:
        return None
    scaled_mad = MAD_GAUSS_CONST * mad
    effective_spread = max(scaled_mad, rel_floor * abs(med), abs_floor)
    # Полностью вырожденный случай (median≈0, нет пола) — z неинформативен.
    if effective_spread < MIN_MAD:
        return None
    z = (current - med) / effective_spread
    return z, med, effective_spread, mad


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
    rel_floor: float = 0.0,
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

        res = _compute_robust_z(
            current_value, baseline_vals,
            rel_floor=rel_floor,
            abs_floor=MIN_ABS_SPREAD_BY_METRIC.get(metric, DEFAULT_MIN_ABS_SPREAD),
        )
        if res is None:
            counters["skipped_no_baseline"] += 1
            continue
        z, median, spread, mad = res
        if abs(z) < warn_thresh:
            continue

        # Volume guard: не плодим observations того же service/metric.
        recent_count = _count_recent_observations(
            db, cast(int, service.id), metric, now,
        )
        if recent_count >= VOLUME_GUARD_MAX_PER_HOUR:
            counters["skipped_volume_guard"] += 1
            continue

        sev = _severity(z, warn_thresh, crit_thresh)
        extras = {
            "method": method,
            "median": median,
            "mad": mad,
            "spread": spread,          # фактический знаменатель z (с учётом пола)
            "rel_floor": rel_floor,
            "baseline_n": len(baseline_vals),
            "warn_thresh": warn_thresh,
            "crit_thresh": crit_thresh,
            "target_hour": target_hour,
        }

        # baseline_mean/baseline_stddev по схеме — пишем median и
        # effective_spread (max(scaled_mad, rel_floor×|median|, abs_floor)).
        # Это фактический разброс, по которому считался z — честно для downstream.
        ok = _insert_idempotent(
            db,
            service_id=cast(int, service.id),
            ts=cast(datetime, anomaly_ts),
            metric=metric,
            value=current_value,
            baseline_mean=median,
            baseline_stddev=spread,
            z_score=z,
            severity=sev,
            extras=extras,
        )
        if ok:
            counters["inserted"] += 1
        else:
            counters["skipped_dup"] += 1

    return counters


def _detect_log_errors_for_service(
    db: Session,
    service: Service,
    now: datetime,
    *,
    warn_thresh: float,
    crit_thresh: float,
    rel_floor: float = 0.0,
) -> Dict[str, int]:
    """Robust-z по объёму Error/Fatal-логов сервиса (из kg_log_observations).

    Серия строится из НЕНУЛЕВЫХ error-бакетов (Error+Fatal, суммированных по
    ts-окну): baseline = «типичный объём ошибок когда они есть», current —
    последние CURRENT_POINTS бакетов. z ловит «ошибок сильно больше обычного».

    Ограничение v1: «впервые ошибся» (пустой baseline / MAD=0) НЕ ловится —
    robust-z требует baseline-разброс. Это всё равно строго лучше нуля
    app-слойных аномалий (http_5xx/p95 в ServiceHealth всегда 0, scrape-gap).
    Флагуем только ПОЛОЖИТЕЛЬНЫЙ z (рост ошибок; падение — не инцидент).
    """
    counters = {
        "inserted": 0, "skipped_dup": 0, "skipped_no_baseline": 0,
        "skipped_no_current": 0, "skipped_volume_guard": 0,
    }
    current_start = now - timedelta(hours=BASELINE_EXCLUDE_HOURS)
    baseline_start = now - timedelta(days=BASELINE_DAYS)

    rows: List[LogObservation] = (
        db.query(LogObservation)
        .filter(
            LogObservation.service_id == service.id,
            LogObservation.level.in_(LOG_ERROR_LEVELS),
            LogObservation.ts >= baseline_start,
            LogObservation.ts <= now,
        )
        .order_by(LogObservation.ts.asc())
        .all()
    )
    if not rows:
        return counters

    # Error+Fatal в одном ts-окне суммируем в один бакет.
    by_ts: Dict[datetime, int] = {}
    for r in rows:
        ts = cast(datetime, r.ts)
        by_ts[ts] = by_ts.get(ts, 0) + int(r.count or 0)
    series = sorted(by_ts.items())  # [(ts, count)] возрастающе

    baseline_vals = [float(c) for ts, c in series if ts < current_start]
    current_buckets = [(ts, c) for ts, c in series if ts >= current_start][-CURRENT_POINTS:]
    if not current_buckets:
        counters["skipped_no_current"] += 1
        return counters

    anomaly_ts = current_buckets[-1][0]
    current_value = statistics.fmean([float(c) for _, c in current_buckets])

    res = _compute_robust_z(
        current_value, baseline_vals,
        rel_floor=rel_floor,
        abs_floor=MIN_ABS_SPREAD_BY_METRIC.get(LOG_ERROR_METRIC, DEFAULT_MIN_ABS_SPREAD),
    )
    if res is None:
        counters["skipped_no_baseline"] += 1
        return counters
    z, median, spread, mad = res
    if z < warn_thresh:  # только рост (положительный z); |z| не нужен
        return counters

    recent = _count_recent_observations(
        db, cast(int, service.id), LOG_ERROR_METRIC, now,
    )
    if recent >= VOLUME_GUARD_MAX_PER_HOUR:
        counters["skipped_volume_guard"] += 1
        return counters

    sev = _severity(z, warn_thresh, crit_thresh)
    extras = {
        "method": "robust_z_log_errors",
        "median": median,
        "mad": mad,
        "spread": spread,
        "rel_floor": rel_floor,
        "baseline_n": len(baseline_vals),
        "levels": list(LOG_ERROR_LEVELS),
        "is_log_proxy": True,  # НЕ HTTP 5xx — лог-производный сигнал
    }
    ok = _insert_idempotent(
        db,
        service_id=cast(int, service.id),
        ts=cast(datetime, anomaly_ts),
        metric=LOG_ERROR_METRIC,
        value=current_value,
        baseline_mean=median,
        baseline_stddev=spread,
        z_score=z,
        severity=sev,
        extras=extras,
    )
    counters["inserted" if ok else "skipped_dup"] += 1
    return counters


def detect_anomalies(
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Beat-task entry — детектировать аномалии по всем real services.

    `now` — для тестов (фиксированное время). Default = datetime.utcnow().
    Помимо robust-z по ServiceHealth-метрикам, прогоняет лог-error детектор
    (log_error_rate) по сервисам с записями в kg_log_observations.
    """
    now = now or datetime.utcnow()
    warn_thresh = _threshold_warn()
    crit_thresh = _threshold_crit()
    rel_floor = _rel_spread_floor()

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
                rel_floor=rel_floor,
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

    # Лог-error аномалии: только сервисы с записями в kg_log_observations за
    # baseline-окно (обычно ~36, не все 2.4k real svc → дёшево).
    log_svc_ids = {
        sid for (sid,) in (
            db.query(LogObservation.service_id)
            .filter(
                LogObservation.ts >= now - timedelta(days=BASELINE_DAYS),
                LogObservation.service_id.isnot(None),
            )
            .distinct()
            .all()
        )
    }
    svc_by_id = {s.id: s for s in services}
    stats["log_error_services"] = len(log_svc_ids)
    for sid in log_svc_ids:
        svc_opt = svc_by_id.get(sid)
        if svc_opt is None:
            continue
        try:
            lc = _detect_log_errors_for_service(
                db, svc_opt, now, warn_thresh=warn_thresh, crit_thresh=crit_thresh,
                rel_floor=rel_floor,
            )
        except Exception as e:
            stats["errors"] += 1
            log.warning(
                "anomaly_detection.log_errors_failed ns=%s name=%s err=%s",
                svc_opt.namespace, svc_opt.name, e,
            )
            continue
        for k in ("inserted", "skipped_dup", "skipped_no_baseline",
                  "skipped_no_current", "skipped_volume_guard"):
            stats[k] += lc[k]

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
