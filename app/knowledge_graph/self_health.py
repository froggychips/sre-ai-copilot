"""KG self-health: «monitoring of the monitoring».

Wave 5 retrospective: с 22 по 23 мая 2026 mem_pct в kg_service_health был
всегда 0% — PromQL many-to-one bug в metrics_sync.py молча давал нули.
Anomaly-detector честно строил baseline на нулях, health_score её ел,
никаких alert'ов не появилось. Этот модуль строит canary'и на сами KG-данные
чтобы такие тихие деградации детектились автоматически.

Каждая проверка возвращает `CheckResult(name, status, detail)` где status ∈
{`ok`, `warn`, `fail`}. Beat-task `kg_self_health_check` агрегирует и пишет в
audit-log (`KG_SELF_HEALTH_OK` / `_WARN` / `_FAIL`); на наличие fail-ов опционально
шлёт single Discord embed в `DISCORD_WEBHOOK_SELF_HEALTH_URL` (отдельный
dev-канал, не #infra-error).

Проверки:
    1. materialization_zero_rate — % строк где метрика = 0/NULL за 24h в
       kg_service_health. Wave 5 reproducer. Allowlist для TODO-метрик
       (http_5xx_rate, p95_latency_ms — пока WO scrape config не на месте).
    2. sync_lag — задержка по beat-задачам (max(ts) / max(created_at)).
       >2× ожидаемого интервала → warn, >5× → fail.
    3. anomaly_signal_health — count(kg_anomaly_observations за 24h).
       0 → warn (либо детектор поломан, либо данные плоские).
       >500 → warn (overload, threshold не работает).
    4. alerts_resolve_freshness — кол-во kg_alerts с fired_at <7d назад и
       resolved_at IS NULL. >20 → warn. Регрессия Wave 1 Track B.
    5. pod_events_link_rate — % kg_pod_events за 24h с service_id NOT NULL.
       <80% warn, <50% fail. StS-резолвер регрессия.
    6. edges_freshness — % kg_service_edges с last_seen_at <24h назад или
       NULL. >30% → warn (kg_sync UPSERT регрессия).

Идемпотентность: само по себе read-only, безопасно к повторным запускам.
Discord-dedup — на уровне beat-задачи (см. tasks.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import structlog
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        ClusterObservation,
                                        LogObservation,
                                        PodEvent, Service, ServiceEdge,
                                        ServiceHealth, SignalAggregate)

log = structlog.get_logger()


# ── Public types ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str           # "ok" | "warn" | "fail"
    detail: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": dict(self.detail)}


# ── Constants ─────────────────────────────────────────────────────────────

# Колонки kg_service_health, которые проверяются на «много нулей».
_SERVICE_HEALTH_METRICS = (
    "cpu_pct",
    "mem_pct",
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
)

# Beat task → (timestamp column factory, expected interval minutes).
# Каждая enrty задаёт «когда последняя запись и какой нормальный период».
# Используется sync_lag check.
_SYNC_LAG_TARGETS: Dict[str, Dict[str, Any]] = {
    "kg_topology_sync": {
        # Topology обновляет Service.updated_at каждый час.
        "column": lambda: func.max(Service.updated_at),
        "interval_minutes": 60,
    },
    "kg_metrics_sync": {
        # ServiceHealth.ts — per-tick, должен идти каждые 10 мин.
        "column": lambda: func.max(ServiceHealth.ts),
        "interval_minutes": 10,
    },
    "kg_cluster_health_sync": {
        "column": lambda: func.max(ClusterObservation.ts),
        "interval_minutes": 5,
    },
    "kg_seq_logs_sync": {
        "column": lambda: func.max(LogObservation.ts),
        "interval_minutes": 10,
    },
    "kg_anomaly_detection_task": {
        # AnomalyObservation.ts — точка детекции. Создаётся не каждый тик,
        # но в живой системе обязан появляться хотя бы за 50 мин.
        "column": lambda: func.max(AnomalyObservation.ts),
        "interval_minutes": 10,
    },
    "kg_signal_aggregates_compute": {
        # SignalAggregate.window_end шагает каждые 24h, но компьютится hourly;
        # для liveness важнее последний компьют — берём window_end как proxy.
        "column": lambda: func.max(SignalAggregate.window_end),
        "interval_minutes": 24 * 60,
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _known_zero_metrics() -> set[str]:
    """Распарсить CSV из settings в set. Trim и нижний регистр."""
    raw = (settings.KG_SELF_HEALTH_KNOWN_ZERO_METRICS or "").strip()
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _now() -> datetime:
    # Все timestamp'ы в БД — naive UTC (datetime.utcnow). Используем тот же
    # формат чтобы сравнение не падало в sqlite/postgres.
    return datetime.utcnow()


# ── Individual checks ─────────────────────────────────────────────────────

def check_materialization_zero_rate(db: Session) -> CheckResult:
    """Wave 5 reproducer: какая доля kg_service_health за 24h = 0 или NULL.

    Для каждой метрики из `_SERVICE_HEALTH_METRICS`:
        zero_rate = SUM(col IS NULL OR col = 0) / COUNT(*)
    Игнорируем метрики из allowlist (TODO в metrics_sync).

    >0.9 → fail, >0.7 → warn, иначе ok.
    """
    known_zero = _known_zero_metrics()
    since = _now() - timedelta(hours=24)

    per_metric: Dict[str, Dict[str, Any]] = {}
    worst_status = "ok"

    total_rows = (
        db.query(func.count(ServiceHealth.id))
        .filter(ServiceHealth.ts >= since)
        .scalar()
    ) or 0

    if total_rows == 0:
        # Нет данных вообще — это вотчина sync_lag check'а, тут возвращаем ok
        # (не дублировать сигнал).
        return CheckResult(
            name="materialization_zero_rate",
            status="ok",
            detail={"reason": "no rows in last 24h", "total_rows": 0},
        )

    for metric in _SERVICE_HEALTH_METRICS:
        col = getattr(ServiceHealth, metric)
        zero_or_null = (
            db.query(func.count(ServiceHealth.id))
            .filter(ServiceHealth.ts >= since)
            .filter(or_(col.is_(None), col == 0))
            .scalar()
        ) or 0
        rate = zero_or_null / total_rows if total_rows else 0.0
        per_metric[metric] = {
            "zero_or_null_pct": round(rate * 100, 1),
            "rows": total_rows,
            "allowlisted": metric in known_zero,
        }
        if metric in known_zero:
            continue
        if rate > 0.9:
            this_status = "fail"
        elif rate > 0.7:
            this_status = "warn"
        else:
            this_status = "ok"
        per_metric[metric]["status"] = this_status
        if this_status == "fail" or (this_status == "warn" and worst_status != "fail"):
            worst_status = this_status

    return CheckResult(
        name="materialization_zero_rate",
        status=worst_status,
        detail={
            "window_hours": 24,
            "total_rows": total_rows,
            "known_zero_allowlist": sorted(known_zero),
            "per_metric": per_metric,
        },
    )


def check_sync_lag(db: Session) -> CheckResult:
    """Свежесть каждой beat-задачи vs ожидаемый интервал.

    Берём last-timestamp из релевантной колонки (см. _SYNC_LAG_TARGETS).
    lag > 5× interval → fail, > 2× → warn. Если данных нет совсем — fail
    (если сервис только что поднят, это всё равно сигнал, чтоб отследить).
    """
    now = _now()
    per_task: Dict[str, Dict[str, Any]] = {}
    worst_status = "ok"

    for task_name, cfg in _SYNC_LAG_TARGETS.items():
        col_factory = cfg["column"]
        interval = cfg["interval_minutes"]
        last_ts: Optional[datetime] = db.query(col_factory()).scalar()
        if last_ts is None:
            per_task[task_name] = {
                "last_ts": None,
                "lag_minutes": None,
                "expected_interval_minutes": interval,
                "status": "fail",
            }
            worst_status = "fail"
            continue
        lag = (now - last_ts).total_seconds() / 60.0
        if lag > 5 * interval:
            this_status = "fail"
        elif lag > 2 * interval:
            this_status = "warn"
        else:
            this_status = "ok"
        per_task[task_name] = {
            "last_ts": last_ts.isoformat(),
            "lag_minutes": round(lag, 1),
            "expected_interval_minutes": interval,
            "status": this_status,
        }
        if this_status == "fail" or (this_status == "warn" and worst_status != "fail"):
            worst_status = this_status

    return CheckResult(
        name="sync_lag",
        status=worst_status,
        detail={"per_task": per_task},
    )


def check_anomaly_signal_health(db: Session) -> CheckResult:
    """Зрелость anomaly-detector'а за последние 24h.

    0 observations → warn (либо threshold mute-ит всё, либо детектор не работает).
    >500 → warn (overload, нужно поднимать threshold).
    """
    since = _now() - timedelta(hours=24)
    count = (
        db.query(func.count(AnomalyObservation.id))
        .filter(AnomalyObservation.ts >= since)
        .scalar()
    ) or 0
    if count == 0:
        status = "warn"
        reason = "no anomaly observations in 24h (detector silent?)"
    elif count > 500:
        status = "warn"
        reason = "anomaly observations >500 in 24h (threshold too sensitive?)"
    else:
        status = "ok"
        reason = "in healthy range [1, 500]"
    return CheckResult(
        name="anomaly_signal_health",
        status=status,
        detail={"count_24h": count, "reason": reason},
    )


def check_alerts_resolve_freshness(db: Session) -> CheckResult:
    """Регрессия Wave 1 Track B: «alerts_resolve замёрз».

    Сколько kg_alerts с fired_at < now()-7d AND resolved_at IS NULL.
    >20 → warn (age-fallback тоже залип).
    """
    cutoff = _now() - timedelta(days=7)
    stale_open = (
        db.query(func.count(AlertEvent.id))
        .filter(AlertEvent.fired_at < cutoff)
        .filter(AlertEvent.resolved_at.is_(None))
        .scalar()
    ) or 0
    if stale_open > 20:
        status = "warn"
        reason = "alerts_resolve_sync likely stuck"
    else:
        status = "ok"
        reason = "within expected range"
    return CheckResult(
        name="alerts_resolve_freshness",
        status=status,
        detail={"stale_open_alerts": stale_open, "older_than_days": 7, "reason": reason},
    )


def check_pod_events_link_rate(db: Session) -> CheckResult:
    """StS-резолвер регрессия: % kg_pod_events с привязанным service_id.

    <80% warn, <50% fail. Окно — 24h (свежие события только).
    """
    since = _now() - timedelta(hours=24)
    total = (
        db.query(func.count(PodEvent.id))
        .filter(PodEvent.first_seen >= since)
        .scalar()
    ) or 0
    if total == 0:
        # Нет событий — sync_lag поймает; здесь не алёртим.
        return CheckResult(
            name="pod_events_link_rate",
            status="ok",
            detail={"total": 0, "reason": "no pod events in 24h"},
        )
    linked = (
        db.query(func.count(PodEvent.id))
        .filter(PodEvent.first_seen >= since)
        .filter(PodEvent.service_id.isnot(None))
        .scalar()
    ) or 0
    rate = linked / total if total else 0.0
    pct = round(rate * 100, 1)
    if rate < 0.5:
        status = "fail"
    elif rate < 0.8:
        status = "warn"
    else:
        status = "ok"
    return CheckResult(
        name="pod_events_link_rate",
        status=status,
        detail={"total": total, "linked": linked, "linked_pct": pct},
    )


def check_edges_freshness(db: Session) -> CheckResult:
    """% рёбер с last_seen_at < now()-24h или NULL.

    >30% → warn. Это regression watch для kg_sync UPSERT — если он перестал
    апдейтить last_seen_at, мы не видим распада топологии до недель.
    """
    cutoff = _now() - timedelta(hours=24)
    total = db.query(func.count(ServiceEdge.id)).scalar() or 0
    if total == 0:
        return CheckResult(
            name="edges_freshness",
            status="ok",
            detail={"total": 0, "reason": "no edges in graph"},
        )
    stale = (
        db.query(func.count(ServiceEdge.id))
        .filter(or_(ServiceEdge.last_seen_at.is_(None), ServiceEdge.last_seen_at < cutoff))
        .scalar()
    ) or 0
    rate = stale / total if total else 0.0
    pct = round(rate * 100, 1)
    status = "warn" if rate > 0.3 else "ok"
    return CheckResult(
        name="edges_freshness",
        status=status,
        detail={"total": total, "stale": stale, "stale_pct": pct},
    )


# ── Orchestrator ──────────────────────────────────────────────────────────

_ALL_CHECKS = (
    check_materialization_zero_rate,
    check_sync_lag,
    check_anomaly_signal_health,
    check_alerts_resolve_freshness,
    check_pod_events_link_rate,
    check_edges_freshness,
)


def run_self_health_checks(db: Session) -> List[CheckResult]:
    """Запустить все проверки в порядке `_ALL_CHECKS`. Возвращает список
    CheckResult — orchestrator (beat-task) сам решает что писать в audit и
    нужно ли уведомление в Discord.

    Read-only — никаких UPDATE/INSERT в KG. Каждая проверка изолирована;
    падение одной не блокирует остальные.
    """
    results: List[CheckResult] = []
    for fn in _ALL_CHECKS:
        try:
            r = fn(db)
            results.append(r)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("self_health.check_failed", check=fn.__name__, error=str(e))
            results.append(CheckResult(
                name=fn.__name__.removeprefix("check_"),
                status="fail",
                detail={"error": f"{type(e).__name__}: {str(e)[:160]}"},
            ))
    return results


def aggregate_status(results: Sequence[CheckResult]) -> str:
    """fail > warn > ok. Если есть хоть один fail — fail; иначе warn если warn'ы; иначе ok."""
    has_warn = False
    for r in results:
        if r.status == "fail":
            return "fail"
        if r.status == "warn":
            has_warn = True
    return "warn" if has_warn else "ok"


def fingerprint(results: Sequence[CheckResult]) -> str:
    """Грубый fingerprint для dedup — sorted имена fail-чеков.

    Если набор fail-ов тот же — это та же проблема, повторно в Discord не шлём.
    Тонкая адаптация (по detail) намеренно не делается: пусть лучше шум-фильтр
    срабатывает, чем мы будем спамить вариациями той же ошибки.
    """
    fails = sorted(r.name for r in results if r.status == "fail")
    return ",".join(fails)
