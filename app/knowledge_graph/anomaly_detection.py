"""Rolling z-score anomaly detection по kg_service_health.

Beat-task `kg_anomaly_detection_task` каждые ~10 мин:
1. Берём все real (synthetic=False) services из KG.
2. Для каждого сервиса:
   - current_window = последние 6 точек (≈1ч при 10-мин ритме) → текущее
     значение метрики = mean этих точек (устойчивее к одной выбросной точке).
   - baseline_window = 7d, исключая последний 1ч (чтобы текущий всплеск не
     подмешивался в baseline).
   - z = (current - mean_baseline) / stddev_baseline.
3. Защиты от ложных срабатываний:
   - baseline_n < 10 → пропускаем метрику (мало данных).
   - stddev < 1e-6 → пропускаем (flat-line, любой шум даст ∞).
   - NULL в value → пропускаем эту метрику для этой точки.
4. |z|>3 → INSERT в kg_anomaly_observations. severity:
     * |z|>5 → 'critical'
     * иначе → 'warning'
5. Идемпотентность: UNIQUE(service_id, ts, metric) + per-row savepoint.

Discord-уведомление в этой фазе не реализуется — пишем notified=false,
вторая фаза обработает unsent.

CLI: `python -m app.knowledge_graph.anomaly_detection`.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (AnomalyObservation, Service,
                                        ServiceHealth)

log = logging.getLogger(__name__)

# Метрики, по которым считаем z-score. Должны существовать в ServiceHealth.
METRICS: Tuple[str, ...] = (
    "cpu_pct",
    "mem_pct",
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
)

# Параметры окон / порогов.
CURRENT_POINTS = 6              # последние ≈1ч при 10-мин ритме
BASELINE_DAYS = 7               # окно baseline
BASELINE_EXCLUDE_HOURS = 1      # исключить последний час из baseline
MIN_BASELINE_POINTS = 10        # ниже — слишком мало, пропускаем
MIN_STDDEV = 1e-6               # защита от division-by-zero / flat-line
Z_WARNING = 3.0
Z_CRITICAL = 5.0


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


def _compute_z(
    current: float, baseline: List[float],
) -> Optional[Tuple[float, float, float]]:
    """Возвращает (z, mean, stddev) или None если baseline недостаточен."""
    if len(baseline) < MIN_BASELINE_POINTS:
        return None
    try:
        mean = statistics.fmean(baseline)
        stddev = statistics.pstdev(baseline)
    except statistics.StatisticsError:
        return None
    if stddev < MIN_STDDEV:
        return None
    z = (current - mean) / stddev
    return z, mean, stddev


def _severity(z: float) -> str:
    return "critical" if abs(z) > Z_CRITICAL else "warning"


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
) -> bool:
    """INSERT с защитой по UNIQUE(service_id, ts, metric).

    Кроссдиалектно: savepoint + IntegrityError. Возвращает True если строка
    реально вставилась.
    """
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
) -> Dict[str, int]:
    """Считает аномалии для одного сервиса.

    Возвращает счётчики: inserted / skipped_dup / skipped_no_baseline /
    skipped_no_current.
    """
    counters = {
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_no_baseline": 0,
        "skipped_no_current": 0,
    }

    current_start = now - timedelta(hours=BASELINE_EXCLUDE_HOURS)
    baseline_start = now - timedelta(days=BASELINE_DAYS)

    # Один SELECT по всему baseline-окну, дальше делим в Python — экономит
    # роунд-трипы при ~370 сервисах. ORDER BY ts ASC чтобы latest легко
    # отрезать срезом.
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

    # Делим на baseline (ts < current_start) и current (последние CURRENT_POINTS
    # точек). Если current-окно пустое — нечего сравнивать, пропускаем.
    baseline_rows = [r for r in rows if r.ts < current_start]
    current_rows = [r for r in rows if r.ts >= current_start][-CURRENT_POINTS:]
    if not current_rows:
        counters["skipped_no_current"] += len(METRICS)
        return counters

    # `ts` для записи аномалии — самая свежая точка current-окна. Это
    # стабильный ключ идемпотентности при пере-запуске на тот же snapshot.
    anomaly_ts = current_rows[-1].ts

    for metric in METRICS:
        current_vals = _extract_values(current_rows, metric)
        baseline_vals = _extract_values(baseline_rows, metric)
        if not current_vals:
            counters["skipped_no_current"] += 1
            continue
        # current = mean current_window → устойчивее к единичной выбросной точке
        # (одна нестабильная scrape-точка не должна давать ложный alert).
        current_value = statistics.fmean(current_vals)

        res = _compute_z(current_value, baseline_vals)
        if res is None:
            counters["skipped_no_baseline"] += 1
            continue
        z, mean, stddev = res
        if abs(z) <= Z_WARNING:
            continue

        ok = _insert_idempotent(
            db,
            service_id=service.id,
            ts=anomaly_ts,
            metric=metric,
            value=current_value,
            baseline_mean=mean,
            baseline_stddev=stddev,
            z_score=z,
            severity=_severity(z),
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

    services: List[Service] = (
        db.query(Service).filter(Service.synthetic.is_(False)).all()
    )
    stats: Dict[str, Any] = {
        "real_services": len(services),
        "now": now.isoformat(),
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_no_baseline": 0,
        "skipped_no_current": 0,
        "errors": 0,
        "by_severity": {"warning": 0, "critical": 0},
    }

    for svc in services:
        try:
            counters = _detect_for_service(db, svc, now)
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

    # Один COMMIT в конце — savepoints внутри уже зафиксировали успешные
    # INSERT-ы. Если COMMIT упадёт — всё откатится, что приемлемо (следующий
    # beat-tick повторит).
    db.commit()

    # Подсчёт by_severity по только что вставленным — дополнительный SELECT
    # дешёвый и даёт быстрый sanity-сигнал в логах. Берём только записи с
    # created_at >= start of this run (≈ now минус safety-зазор 1 мин).
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
        # Не критично — лог-deко.
        pass

    log.info(
        "anomaly_detection.done real=%d inserted=%d (warn=%d crit=%d) "
        "skipped_dup=%d skipped_no_baseline=%d skipped_no_current=%d errors=%d",
        stats["real_services"], stats["inserted"],
        stats["by_severity"]["warning"], stats["by_severity"]["critical"],
        stats["skipped_dup"], stats["skipped_no_baseline"],
        stats["skipped_no_current"], stats["errors"],
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(detect_anomalies(db))
    finally:
        db.close()
