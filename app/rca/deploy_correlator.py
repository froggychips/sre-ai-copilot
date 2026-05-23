"""Deploy correlator — связь между alert/incident и недавним deploy сервиса.

При срабатывании alert ищем ближайший по времени deploy того же сервиса
(в окне lookback_hours до инцидента) и сравниваем «до vs после» по метрикам
из kg_service_health. Если хоть одна метрика подскочила более чем на 50% —
выдаём verdict=suspect.

Используется RCA-пайплайном как дополнительный сигнал «релиз сломал прод».
Read-only: только select-ы, никаких commit-ов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

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

# Порог, при превышении которого относим деплой к suspect.
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


def _strip_tz(dt: datetime) -> datetime:
    """kg_service_health.ts хранится как naive UTC — приводим к тому же виду."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _avg(rows, attr: str) -> Optional[float]:
    """Среднее по атрибуту attr, пропуская None. None если нет данных."""
    values = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _delta_pct(before: Optional[float], after: Optional[float],
               metric: str) -> Optional[float]:
    """Относительное изменение в процентах с защитой от before≈0.

    Возвращает None, если данных недостаточно для вывода.
    """
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
                "cpu_pct": {"before": .., "after": .., "delta_pct": ..},
                ...
              },
              "verdict": "suspect" | "ok",
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

    metrics_diff: Dict[str, Dict[str, Optional[float]]] = {}
    verdict = "ok"
    for metric in _METRICS:
        before_avg = _avg(before_rows, metric)
        after_avg = _avg(after_rows, metric)
        delta = _delta_pct(before_avg, after_avg, metric)
        metrics_diff[metric] = {
            "before": before_avg,
            "after": after_avg,
            "delta_pct": delta,
        }
        # suspect если любая метрика выросла > порога. Падение игнорируем
        # (для всех 5 метрик «меньше — лучше», падение это нормально).
        if delta is not None and delta > _SUSPECT_DELTA_PCT:
            verdict = "suspect"

    extras = deploy.extras if isinstance(deploy.extras, dict) else {}
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
        "minutes_before_incident": int(
            (incident_naive - deploy_ts).total_seconds() // 60
        ),
    }

    return {
        "deploy": deploy_dict,
        "metrics_diff": metrics_diff,
        "verdict": verdict,
    }
