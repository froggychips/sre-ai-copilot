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
       >2× ожидаемого интервала → warn, >5× → fail. Для задач, чей результат
       не обязан появляться каждый тик (anomaly detection), опора — redis-
       heartbeat ПРОГОНА задачи, а не max(ts) результата.
    3. anomaly_signal_health — count(kg_anomaly_observations за 24h).
       0 → warn (либо детектор поломан, либо данные плоские).
       >500 → warn (overload, threshold не работает).
    4. alerts_resolve_freshness — кол-во kg_alerts с fired_at <7d назад и
       resolved_at IS NULL. >20 → warn. Регрессия Wave 1 Track B.
    5. pod_events_link_rate — % kg_pod_events за 24h с service_id NOT NULL.
       <80% warn, <50% fail. StS-резолвер регрессия.
    6. edges_freshness — % kg_service_edges с last_seen_at <24h назад или
       NULL. >30% → warn (kg_sync UPSERT регрессия).
    7. graph_integrity — структурные инварианты (должны быть 0): фантом-дубли
       db-узлов (#185/#189), serves_traffic self-loops (#190), висячие рёбра.
       >0 → fail/warn. Regression-watch для багов, вычищенных в rc.2.

Идемпотентность: само по себе read-only, безопасно к повторным запускам.
Discord-dedup — на уровне beat-задачи (см. tasks.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import structlog
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        ClusterObservation, Deployment,
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
#
# `column`      — max(timestamp) материализованных данных задачи;
# `heartbeat_task` — если задан, свежесть берётся из redis-heartbeat самой
#                 задачи (факт ПРОГОНА), а `column` остаётся информационным.
#                 Нужно там, где результат задачи не обязан появляться каждый
#                 тик (см. kg_anomaly_detection_task).
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
        # Свежесть — по ФАКТУ ПРОГОНА (redis-heartbeat), а не по наличию
        # результата. Раньше брался max(AnomalyObservation.ts) с интервалом
        # 10 мин → fail при >50 мин без единой аномалии. Но аномалия — это
        # РЕЗУЛЬТАТ: после noise-floor и volume guard тихий час абсолютно
        # нормален, и check_anomaly_signal_health тот же ноль (уже за 24h!)
        # трактует лишь как warn. Две проверки противоречили друг другу, а
        # fingerprint-dedup (по набору fail-ов) закреплял этот ложный fail
        # в Discord надолго. Heartbeat пишется task_postrun-сигналом только
        # для успешных прогонов (см. tasks._record_beat_heartbeat), т.е.
        # «детектор реально ходит» — именно то, что мы хотим проверить.
        "heartbeat_task": "kg_anomaly_detection_task",
        "interval_minutes": 10,
        # Информационно (в статус не входит): когда последний раз ЧТО-ТО нашли.
        "column": lambda: func.max(AnomalyObservation.ts),
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


def _last_heartbeat(task_name: str) -> Optional[datetime]:
    """Время последнего успешного прогона beat-задачи (naive UTC) или None.

    Тот же механизм, что у дайджеста (check_digest_delivery): redis-ключ
    `stats:beat:last_run:<task>`, который пишет `task_postrun`-сигнал
    (app/workers/tasks._record_beat_heartbeat) только для SUCCESS-прогонов
    без error-маркера в retval.

    В redis лежит tz-aware ISO, а весь этот модуль работает в naive UTC —
    нормализуем, иначе вычитание падает с "can't subtract offset-naive and
    offset-aware datetimes". Fail-open: redis недоступен / ключа нет → None.
    """
    try:
        from app.services.stats_digest import _get_beat_last_run
        last = _get_beat_last_run(task_name)
    except Exception as e:  # pragma: no cover — defensive (redis/import)
        log.warning("self_health.heartbeat_read_failed", task=task_name, error=str(e))
        return None
    if last is None:
        return None
    if last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    return last


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

    Берём last-timestamp из релевантной колонки (см. _SYNC_LAG_TARGETS), а для
    задач с `heartbeat_task` — из redis-heartbeat самой задачи (факт прогона,
    а не наличие результата).
    lag > 5× interval → fail, > 2× → warn. Если данных нет совсем — fail
    (если сервис только что поднят, это всё равно сигнал, чтоб отследить).
    Исключение: у heartbeat-задач отсутствие ключа = warn, а не fail —
    «неизвестно» (redis перезапускался / инстанс только поднят) не то же
    самое, что «задача не ходит».
    """
    now = _now()
    per_task: Dict[str, Dict[str, Any]] = {}
    worst_status = "ok"

    for task_name, cfg in _SYNC_LAG_TARGETS.items():
        interval = cfg["interval_minutes"]
        hb_task = cfg.get("heartbeat_task")
        entry: Dict[str, Any] = {
            "expected_interval_minutes": interval,
            "source": "heartbeat" if hb_task else "data_ts",
        }
        last_ts: Optional[datetime]
        if hb_task:
            last_ts = _last_heartbeat(hb_task)
            col_factory = cfg.get("column")
            if col_factory is not None:
                # Информационно: когда задача последний раз что-то материализовала.
                data_ts: Optional[datetime] = db.query(col_factory()).scalar()
                entry["last_result_ts"] = (
                    data_ts.isoformat() if data_ts is not None else None
                )
            if last_ts is None:
                entry.update({
                    "last_ts": None,
                    "lag_minutes": None,
                    "status": "warn",
                    "reason": "нет heartbeat в redis (ключ не найден/redis недоступен)",
                })
                per_task[task_name] = entry
                if worst_status != "fail":
                    worst_status = "warn"
                continue
        else:
            last_ts = db.query(cfg["column"]()).scalar()
            if last_ts is None:
                entry.update({
                    "last_ts": None,
                    "lag_minutes": None,
                    "status": "fail",
                })
                per_task[task_name] = entry
                worst_status = "fail"
                continue
        lag = (now - last_ts).total_seconds() / 60.0
        if lag > 5 * interval:
            this_status = "fail"
        elif lag > 2 * interval:
            this_status = "warn"
        else:
            this_status = "ok"
        entry.update({
            "last_ts": last_ts.isoformat(),
            "lag_minutes": round(lag, 1),
            "status": this_status,
        })
        per_task[task_name] = entry
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


def check_deploy_stream_ingestion(db: Session) -> CheckResult:
    """TC отдаёт deploy-билды для известных KG-namespace'ов, а в kg_deployments
    их нет → ingestion сломан.

    Зачем отдельный чек (а не sync_lag по max(started_at)): freshness не
    отличает реальный сбой ingestion от тихого периода без деплоев (gap
    выходных ~60ч > 36ч-сбоя 2026-06-06 из-за веток '<default>'). Этот чек
    семантический и независим от каденса: если TC за 24h вернул N deploy-
    билдов для KG-веток, а в KG присутствует <50% — fail (>0% — warn). Если
    «should-ingest» билдов 0 (тихо) — ok. TC недоступен → ok/skip (это вотчина
    отдельного мониторинга, не наша).
    """
    try:
        import asyncio

        from app.services.teamcity_service import (branch_for_namespace,
                                                   recent_deploys)
        builds = asyncio.run(recent_deploys(lookback_hours=24, limit=200))
    except Exception as e:  # TC не настроен / недоступен — не наш сигнал
        return CheckResult(
            name="deploy_stream_ingestion",
            status="ok",
            detail={"skipped": f"TC unavailable: {type(e).__name__}: {str(e)[:120]}"},
        )
    if not builds:
        return CheckResult(
            name="deploy_stream_ingestion", status="ok",
            detail={"reason": "TC вернул 0 builds (не настроен / тихо)"},
        )

    # branch → list[ns] (обратное к branch_for_namespace по distinct ns в KG)
    ns_by_branch: Dict[str, bool] = {}
    for (ns,) in db.query(Service.namespace).distinct().all():
        br = branch_for_namespace(ns)
        if br:
            ns_by_branch[br] = True

    should_ingest = []
    for b in builds:
        branch = (b.get("branch") or "").replace("refs/heads/", "")
        # Та же нормализация, что в tc_deploys_to_kg (_tc_deploys_to_kg_logic):
        # '<default>' deploy-конфигов (не prod) == preprod.
        if branch == "<default>" and "Prod_" not in (b.get("buildtype_id") or ""):
            branch = "preprod"
        if ns_by_branch.get(branch):
            should_ingest.append(b)

    if not should_ingest:
        return CheckResult(
            name="deploy_stream_ingestion", status="ok",
            detail={"reason": "нет deploy-ветко-билдов в окне 24h",
                    "tc_builds": len(builds)},
        )

    present = 0
    for b in should_ingest:
        exists = (
            db.query(Deployment.id)
            .filter(Deployment.buildtype_id == b.get("buildtype_id"),
                    Deployment.build_number == str(b.get("number") or ""))
            .first()
        )
        if exists:
            present += 1
    missing = len(should_ingest) - present
    miss_rate = missing / len(should_ingest)
    if miss_rate > 0.5:
        status = "fail"
    elif miss_rate > 0.0:
        status = "warn"
    else:
        status = "ok"
    return CheckResult(
        name="deploy_stream_ingestion",
        status=status,
        detail={
            "should_ingest": len(should_ingest),
            "present_in_kg": present,
            "missing": missing,
            "miss_pct": round(miss_rate * 100, 1),
        },
    )


# Структурная целостность графа — порог dangling, выше которого fail (а не warn).
_GRAPH_INTEGRITY_FAIL_DANGLING = 50


def check_graph_integrity(db: Session) -> CheckResult:
    """Структурные инварианты графа — regression-watch для багов, вычищенных
    в rc.2 (2026-06-26). Эти величины ДОЛЖНЫ быть 0 по построению:

      * db_phantom_dup_names — `db:%`-узлы с одним именем в >1 namespace.
        #185 (sync-guard) + #189 (backfill) свели 288→16. >0 = guard-регрессия
        (один физический кластер БД снова размножается в per-ns копии).
      * serves_traffic_self_loops — рёбра src_id==dst_id kind='serves_traffic'.
        #190 guard (`dep_node.id==svc_node.id → skip`) гарантирует 0. >0 =
        регрессия (Service≡Deployment снова плодит петлю → шум в blast-radius).
      * self_loops_any — любые петли (ловит новые edge-builder'ы с тем же багом).
      * dangling_edges — рёбра, чей src/dst отсутствует в kg_services (битый
        delete/merge). Транзиентно при гонке sync → warn; массово → fail.

    fail при dup_names>0 ИЛИ self_loops_any>0 ИЛИ dangling>порога; warn при
    dangling>0; иначе ok. Read-only.
    """
    dup_sub = (
        db.query(Service.name)
        .filter(Service.name.like("db:%"))
        .group_by(Service.name)
        .having(func.count(Service.id) > 1)
        .subquery()
    )
    db_phantom_dup_names = db.query(func.count()).select_from(dup_sub).scalar() or 0

    self_loops_any = (
        db.query(func.count(ServiceEdge.id))
        .filter(ServiceEdge.src_id == ServiceEdge.dst_id)
        .scalar()
    ) or 0
    serves_traffic_self_loops = (
        db.query(func.count(ServiceEdge.id))
        .filter(ServiceEdge.src_id == ServiceEdge.dst_id,
                ServiceEdge.kind == "serves_traffic")
        .scalar()
    ) or 0

    src_a = aliased(Service)
    dst_a = aliased(Service)
    dangling_edges = (
        db.query(func.count(ServiceEdge.id))
        .outerjoin(src_a, ServiceEdge.src_id == src_a.id)
        .outerjoin(dst_a, ServiceEdge.dst_id == dst_a.id)
        .filter(or_(src_a.id.is_(None), dst_a.id.is_(None)))
        .scalar()
    ) or 0

    if (db_phantom_dup_names > 0 or self_loops_any > 0
            or dangling_edges > _GRAPH_INTEGRITY_FAIL_DANGLING):
        status = "fail"
    elif dangling_edges > 0:
        status = "warn"
    else:
        status = "ok"

    return CheckResult(
        name="graph_integrity",
        status=status,
        detail={
            "db_phantom_dup_names": db_phantom_dup_names,
            "self_loops_any": self_loops_any,
            "serves_traffic_self_loops": serves_traffic_self_loops,
            "dangling_edges": dangling_edges,
            "dangling_fail_threshold": _GRAPH_INTEGRITY_FAIL_DANGLING,
        },
    )


# ── Orchestrator ──────────────────────────────────────────────────────────

def check_digest_delivery(db: Session) -> CheckResult:
    """Deadman на сам дайджест: доехал ли он до Discord за последние сутки.

    Мотивация 07.08.2026: дайджест не отправился, и единственным способом
    это заметить был взгляд человека в канал — «сообщения нет» неотличимо от
    «ещё не пришло». Здесь опорой служит copilot_task_runs.last_success_at,
    который пишется ПОСЛЕ фактической отправки в Discord.

    Порог: расписание суточное (STATS_DIGEST_HOUR_UTC), поэтому норма — до
    ~24ч. Больше 26ч (сутки + 2ч на очередь воркера, дайджест реально
    стартует с задержкой 5-10 минут и считается ещё ~10) → пропуск дня, fail.
    Отсутствие успехов вообще — warn, а не fail: свежеподнятый инстанс или
    выключенный флаг не должны выглядеть как поломка.
    """
    from app.services.stats_digest import DIGEST_DELIVERY_TASK, _get_beat_last_run

    if not getattr(settings, "STATS_DIGEST_ENABLED", False):
        return CheckResult(
            name="digest_delivery",
            status="ok",
            detail={"skipped": "STATS_DIGEST_ENABLED=false"},
        )

    last = _get_beat_last_run(DIGEST_DELIVERY_TASK)
    age_min: Optional[float] = None
    if last is not None:
        # В redis heartbeat лежит tz-aware ISO (datetime.now(timezone.utc)),
        # а _now() в этом модуле — naive UTC, как и DateTime-колонки проекта.
        # Без нормализации вычитание падает с "can't subtract offset-naive
        # and offset-aware datetimes".
        if last.tzinfo is not None:
            last = last.astimezone(timezone.utc).replace(tzinfo=None)
        age_min = (_now() - last).total_seconds() / 60.0
    if age_min is None:
        return CheckResult(
            name="digest_delivery",
            status="warn",
            detail={
                "reason": "нет маркера доставки в redis "
                          f"({DIGEST_DELIVERY_TASK}) — дайджест ещё не отправлялся "
                          "или redis перезапускался",
            },
        )

    threshold_min = 26 * 60
    status = "fail" if age_min > threshold_min else "ok"
    return CheckResult(
        name="digest_delivery",
        status=status,
        detail={
            "last_success_age_minutes": round(age_min, 1),
            "threshold_minutes": threshold_min,
        },
    )


_ALL_CHECKS = (
    check_materialization_zero_rate,
    check_sync_lag,
    check_digest_delivery,
    check_anomaly_signal_health,
    check_alerts_resolve_freshness,
    check_pod_events_link_rate,
    check_edges_freshness,
    check_deploy_stream_ingestion,
    check_graph_integrity,
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
