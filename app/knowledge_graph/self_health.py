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
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.knowledge_graph.contract import shared_namespace_of
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        ClusterObservation, Deployment,
                                        LogObservation, Namespace,
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

# Что для колонки значит ноль. Замер на проде 20.08.2026 за 24 часа:
#
#     метрика          NULL     нулей   положительных
#     restarts_rate      14    358 995           3 668
#     cpu_pct         1 377        135         361 165
#     http_5xx_rate 362 677          —               —
#     p95_latency_ms 362 677         —               —
#
# `restarts_rate` — счётчик событий, и 99% нулей у него означают, что поды
# не перезапускались. Правило «>90% нулей = fail» объявляло это поломкой
# материализации, хотя метрика писалась исправно и 3668 раз поймала
# настоящие рестарты: проверка постоянно висела в fail на здоровых данных.
# Настоящая поломка счётчика выглядит иначе — как NULL, то есть «значение
# не записали вовсе», и NULL'ов там ровно 14 из 362 677.
#
# Для gauge (cpu/mem) ноль по-прежнему подозрителен: сервис, потребляющий
# ровно ноль процессора, — это скорее сбой сбора, чем факт. Поэтому колонки
# разделены по смыслу, а не проверяются одним правилом.
#
# 5xx и p95 не пишутся вообще (NULL во всех строках) — они в allowlist
# `KG_SELF_HEALTH_KNOWN_ZERO_METRICS`, пока WO scrape config не подключит
# nginx_ingress-метрики. С разделением по классу allowlist для них
# продолжает работать: NULL-доля 100% дала бы fail без него.
_EVENT_RATE_METRICS = frozenset({
    "restarts_rate",
    "http_5xx_rate",
    "p95_latency_ms",
})

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
        # Свежесть — по ФАКТУ ОПРОСА, а не по наличию записей, той же
        # логикой, что у kg_anomaly_detection_task. Запись появляется только
        # когда в окне были Error/Fatal/Warning, а замер на проде
        # 21.08.2026 показал, что примерно половина 10-минутных окон пусты:
        # за час на shared-инстансе 7 событий, но в двух окнах подряд ноль.
        # Порог fail здесь 5×interval = 50 минут, и тихая ночь дала бы
        # ложный fail.
        #
        # Heartbeat при этом не прячет настоящую слепоту: синк возвращает
        # error-маркер, когда не ответил ни один инстанс, а
        # `_record_beat_heartbeat` для таких прогонов heartbeat не пишет.
        # Именно так ловится случай 20.08.2026 — NetworkPolicy перекрыла
        # доступ ко всем восьми инстансам, и данных не было 12,8 часа.
        "heartbeat_task": "kg_seq_logs_sync",
        "interval_minutes": 10,
        # Информационно (в статус не входит): когда последний раз ЧТО-ТО
        # записали.
        "column": lambda: func.max(LogObservation.ts),
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
    # Источники отдельных видов рёбер. Проверяются ПО ФАКТУ ПРОГОНА: у них
    # нет своей колонки-таймстампа, а свежесть их результата ловит
    # check_edge_kind_freshness. Две проверки отвечают на разные вопросы —
    # «таск ходит» и «данные обновились», и расхождение между ними как раз
    # и есть интересный случай (ходит, но ничего не пишет).
    "kg_nats_subjects_sync": {
        "heartbeat_task": "kg_nats_subjects_sync",
        # Расписание задачи — crontab(minute=43, hour="*/6"), то есть раз в
        # шесть часов. Здесь стояло 60, и проверка ждала прогона каждый час:
        # порог warn = 2×interval, fail = 5×interval, так что при реальном
        # периоде 360 минут warn держался почти постоянно, а fail приходил
        # при любой задержке. Замер 21.08.2026 показал lag=150.6 при
        # совершенно здоровом синке.
        #
        # Расхождение теперь ловится тестом
        # `test_sync_lag_intervals_match_beat_schedule`, а не сверкой глазами.
        "interval_minutes": 360,
    },
    "kg_topology_resources_sync": {
        "heartbeat_task": "kg_topology_resources_sync",
        "interval_minutes": 30,
    },
    "kg_ingress_sync": {
        "heartbeat_task": "kg_ingress_sync",
        "interval_minutes": 60,
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

    # Знаменатель и все числители — ОДНИМ запросом, одним снимком данных.
    # Раньше total_rows считался отдельно и раньше остальных, а метрики —
    # по одному запросу на колонку. Между запросами metrics_sync успевал
    # вставить строки, и доля выходила больше 100%: замер на проде
    # 21.08.2026 показал `http_5xx_rate missing=100.5%`. Число само по себе
    # безобидное, но проверка, печатающая невозможную величину, перестаёт
    # быть свидетельством.
    row = db.query(
        func.count(ServiceHealth.id),
        *[
            func.sum(
                case(
                    (
                        getattr(ServiceHealth, m).is_(None)
                        if m in _EVENT_RATE_METRICS
                        else or_(getattr(ServiceHealth, m).is_(None),
                                 getattr(ServiceHealth, m) == 0),
                        1,
                    ),
                    else_=0,
                )
            )
            for m in _SERVICE_HEALTH_METRICS
        ],
    ).filter(ServiceHealth.ts >= since).one()

    total_rows = int(row[0] or 0)
    missing_by_metric = {
        m: int(row[i + 1] or 0) for i, m in enumerate(_SERVICE_HEALTH_METRICS)
    }

    if total_rows == 0:
        # Нет данных вообще — это вотчина sync_lag check'а, тут возвращаем ok
        # (не дублировать сигнал).
        return CheckResult(
            name="materialization_zero_rate",
            status="ok",
            detail={"reason": "no rows in last 24h", "total_rows": 0},
        )

    for metric in _SERVICE_HEALTH_METRICS:
        # Для счётчика событий «пропало» значит NULL: ноль там — законный
        # ответ «ничего не произошло». Для gauge ноль тоже подозрителен.
        is_event_rate = metric in _EVENT_RATE_METRICS
        missing = missing_by_metric[metric]
        rate = missing / total_rows if total_rows else 0.0
        per_metric[metric] = {
            "missing_pct": round(rate * 100, 1),
            "criterion": "null_only" if is_event_rate else "null_or_zero",
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


#: Ожидаемый интервал обновления рёбер каждого вида и таск, который их пишет.
#: Интервал — не расписание таска, а «через сколько молчание становится
#: подозрительным»: берём с запасом ×3 от периода синка, чтобы один
#: пропущенный тик не поднимал шум.
_EDGE_KIND_SOURCES: Dict[str, Dict[str, Any]] = {
    "calls": {"task": "kg_topology_sync", "stale_after_minutes": 180},
    "uses_db": {"task": "kg_topology_sync", "stale_after_minutes": 180},
    "uses_nats": {"task": "kg_nats_subjects_sync", "stale_after_minutes": 180},
    "serves_traffic": {"task": "kg_topology_resources_sync", "stale_after_minutes": 120},
    "routes_to": {"task": "kg_ingress_sync", "stale_after_minutes": 180},
}


def check_edge_kind_freshness(db: Session) -> CheckResult:
    """Свежесть рёбер ПО ВИДАМ — deadman на источник каждого вида.

    Зачем отдельно от `check_edges_freshness`: та считает долю просроченных
    рёбер по ВСЕМУ графу с порогом 30%, и по арифметике не может заметить
    смерть отдельного источника. Замер 14.08.2026:

        serves_traffic  5336  31.2%
        uses_db         4459  26.0%
        uses_nats       3560  20.8%
        calls           2096  12.2%
        routes_to       1672   9.8%

    Полная остановка NATS-синка даёт 20.8% просроченных рёбер — ниже порога,
    то есть агрегатная проверка промолчит и через сутки, и через неделю.
    Заметен ей только тотальный отказ kg_sync целиком. Именно этот класс
    слепоты («сигнал есть, но не про то») уже стоил суток простоя
    deploy-stream: проверка существовала и рапортовала ok.

    Здесь смотрим на max(last_seen_at) КАЖДОГО вида отдельно и называем
    таск, который его пишет, — чтобы алерт говорил, что чинить, а не «в
    графе что-то не так».

    Вид, которого в графе нет вообще, — не fail: `routes_to` появился
    позже остальных, и пустой kind у свежей инсталляции нормален.
    """
    now = _now()
    per_kind: Dict[str, Dict[str, Any]] = {}
    worst = "ok"

    rows = (
        db.query(ServiceEdge.kind, func.max(ServiceEdge.last_seen_at), func.count(ServiceEdge.id))
        .group_by(ServiceEdge.kind)
        .all()
    )
    seen = {kind: (last_ts, count) for kind, last_ts, count in rows}

    for kind, cfg in _EDGE_KIND_SOURCES.items():
        threshold = int(cfg["stale_after_minutes"])
        entry: Dict[str, Any] = {
            "writer_task": cfg["task"],
            "stale_after_minutes": threshold,
        }
        if kind not in seen:
            # Вида нет вовсе — нечего проверять на свежесть.
            entry.update({"edges": 0, "status": "ok", "reason": "kind отсутствует в графе"})
            per_kind[kind] = entry
            continue

        last_ts, count = seen[kind]
        entry["edges"] = int(count or 0)
        if last_ts is None:
            entry.update({
                "last_seen_at": None,
                "lag_minutes": None,
                "status": "fail",
                "reason": "у всех рёбер вида last_seen_at IS NULL",
            })
            per_kind[kind] = entry
            worst = "fail"
            continue

        # Все timestamp'ы в БД — naive UTC (см. _now), приводить нечего.
        lag = (now - last_ts).total_seconds() / 60.0
        entry["last_seen_at"] = last_ts.isoformat()
        entry["lag_minutes"] = round(lag, 1)
        if lag > threshold * 2:
            entry["status"] = "fail"
            worst = "fail"
        elif lag > threshold:
            entry["status"] = "warn"
            if worst != "fail":
                worst = "warn"
        else:
            entry["status"] = "ok"
        per_kind[kind] = entry

    unknown = sorted(set(seen) - set(_EDGE_KIND_SOURCES))
    return CheckResult(
        name="edge_kind_freshness",
        status=worst,
        detail={
            "per_kind": per_kind,
            # Вид рёбер, которого нет в карте источников: кто-то завёл новый
            # kind, не описав, какой таск его пишет — deadman на него не
            # распространяется, и это стоит видеть.
            "unmapped_kinds": unknown,
        },
    )


def check_schema_version(db: Session) -> CheckResult:
    """Схема БД совпадает с той, которую ожидает выкаченный код.

    Прецедент 14.08.2026: образ rc.19 нёс код с колонкой `owner_source`, а
    миграции в проде не применили — рецепт релиза их просто не содержал.
    kg_topology_sync падал на ВСЕХ 129 namespace с
    `column "owner_source" does not exist`, за тик писал ноль узлов и ноль
    рёбер. Заметили это не по алерту, а случайно: единственным «сигналом»
    была подозрительная скорость тика (40 секунд вместо минут).

    Спасло тогда то, что deadman у edge_decay увидел fetch-ошибки и не стал
    удалять 17 тысяч рёбер, решив, что топология исчезла. То есть данные
    уцелели по счастливой случайности архитектуры, а не потому, что кто-то
    следил за версией схемы.

    Проверка сравнивает `alembic_version` в БД со списком ревизий, которые
    знает код. Отставание — fail: код ожидает объекты, которых в БД нет.
    Опережение — warn: БД новее кода, это бывает при откате образа и само по
    себе не ломает (новые колонки просто не используются).
    """
    from sqlalchemy import text

    try:
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as e:  # noqa: BLE001 — таблицы может не быть в тестовой БД
        return CheckResult(
            name="schema_version",
            status="warn",
            detail={"reason": f"alembic_version недоступна: {type(e).__name__}"},
        )

    expected = _expected_head_revision()
    entry: Dict[str, Any] = {"db_revision": current, "code_head": expected}

    if expected is None:
        entry["reason"] = "не удалось прочитать ревизии из alembic/versions"
        return CheckResult(name="schema_version", status="warn", detail=entry)
    if current == expected:
        return CheckResult(name="schema_version", status="ok", detail=entry)

    known = _known_revisions()
    if current in known and expected in known:
        behind = known.index(current) < known.index(expected)
    else:
        behind = True
    entry["direction"] = "БД отстаёт от кода" if behind else "БД новее кода"
    return CheckResult(
        name="schema_version",
        status="fail" if behind else "warn",
        detail=entry,
    )


def _revision_files() -> List[Any]:
    from pathlib import Path
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    return sorted(versions.glob("*.py")) if versions.exists() else []


def _known_revisions() -> List[str]:
    """Ревизии в порядке имён файлов — они датированы, значит хронологичны."""
    import re
    out: List[str] = []
    for f in _revision_files():
        m = re.search(r'^revision = "([^"]+)"', f.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            out.append(m.group(1))
    return out


def _expected_head_revision() -> Optional[str]:
    """Ревизия, на которую не ссылается ни один down_revision, — это head."""
    import re
    revisions, downs = [], set()
    for f in _revision_files():
        text_ = f.read_text(encoding="utf-8")
        m = re.search(r'^revision = "([^"]+)"', text_, re.MULTILINE)
        d = re.search(r'^down_revision = "([^"]+)"', text_, re.MULTILINE)
        if m:
            revisions.append(m.group(1))
        if d:
            downs.add(d.group(1))
    heads = [r for r in revisions if r not in downs]
    return heads[0] if len(heads) == 1 else None


def _run_coro_blocking(coro):
    """Выполнить корутину из СИНХРОННОГО чека, где бы он ни вызывался.

    `run_self_health_checks` синхронный, но beat-таск оборачивает его в
    `asyncio.run(_kg_self_health_logic())` — то есть event loop уже работает,
    и обычный `asyncio.run()` внутри чека падает с RuntimeError
    «cannot be called from a running event loop».

    Инцидент 2026-08-11: из-за этого `deploy_stream_ingestion` не выполнялся
    НИ РАЗУ — каждый прогон уходил в except и (по прежней fail-open логике)
    докладывал ok со строкой «TC unavailable: RuntimeError: asyncio.run()...».
    В логах при этом висело `RuntimeWarning: coroutine 'recent_deploys' was
    never awaited`. Проверка, написанная ради ловли мёртвого ingestion, сама
    была мертва — и пропустила сутки простоя.

    Внутри loop считаем в отдельном потоке со своим loop: корутина ещё не
    ожидалась, поэтому безопасно переносится.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def check_deploy_stream_ingestion(db: Session) -> CheckResult:
    """TC отдаёт deploy-билды для известных KG-namespace'ов, а в kg_deployments
    их нет → ingestion сломан.

    Зачем отдельный чек (а не sync_lag по max(started_at)): freshness не
    отличает реальный сбой ingestion от тихого периода без деплоев (gap
    выходных ~60ч > 36ч-сбоя 2026-06-06 из-за веток '<default>'). Этот чек
    семантический и независим от каденса: если TC за 24h вернул N deploy-
    билдов для KG-веток, а в KG присутствует <50% — fail (>0% — warn). Если
    «should-ingest» билдов 0 (тихо) — ok.

    ИСТОРИЯ FAIL-OPEN (инцидент 2026-08-11): раньше и «TC недоступен», и «TC
    вернул 0 builds» давали status=ok — «это вотчина отдельного мониторинга».
    Отдельного мониторинга не оказалось: поток деплоев стоял с 10.08 по 11.08,
    чек всё это время докладывал ok, а Discord-атрибуция уверенно писала
    «деплоев не было — вряд ли связано с деплоем» на алерте, прилетевшем через
    20 секунд после прод-раскатки. Теперь молчание источника — сигнал:
      * TC настроен, но упал/отдал 0  → fail (ослепший синк);
      * TC не настроен (нет URL/токена/проектов) → warn с явной причиной,
        чтобы не путать выключенную интеграцию с поломкой.
    """
    from app.services.teamcity_service import tc_sync_config_status
    cfg = tc_sync_config_status()
    try:
        from app.services.teamcity_service import (branch_for_namespace,
                                                   recent_deploys)
        builds = _run_coro_blocking(recent_deploys(lookback_hours=24, limit=200))
    except Exception as e:
        return CheckResult(
            name="deploy_stream_ingestion",
            status="fail" if cfg["configured"] else "warn",
            detail={
                "error": f"TC unavailable: {type(e).__name__}: {str(e)[:120]}",
                "tc_configured": cfg["configured"],
                "hint": "deploy-атрибуция инцидентов слепа, пока источник молчит",
            },
        )
    if not builds:
        if cfg["configured"]:
            return CheckResult(
                name="deploy_stream_ingestion", status="fail",
                detail={
                    "reason": "TC настроен, но вернул 0 deploy-builds за 24h",
                    "hint": "ослепший синк: проверь TC_TOKEN (401 не отличим от "
                            "пустоты), TC_PROJECT_IDS и фильтр _is_deploy_buildtype_name",
                },
            )
        return CheckResult(
            name="deploy_stream_ingestion", status="warn",
            detail={"reason": f"pull деплоев не настроен: {cfg['reason']}"},
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
        # прод-конфиг → prod независимо от ветки (запускается с preprod, где
        # лежит только инструментарий); '<default>' у остальных == preprod.
        # Расхождение с задачей недопустимо: чек сравнивает СВОЙ should_ingest
        # с тем, что записала задача, и разная нормализация дала бы вечный fail.
        from app.services.teamcity_service import is_prod_buildtype
        if is_prod_buildtype(b.get("buildtype_id")):
            branch = "prod"
        elif branch == "<default>":
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

# Порог для рёбер «живой сервис → база в отсутствующем namespace». Единицы
# ожидаемы в момент удаления окружения: sync успел записать ребро, а
# namespace уже пропал — до следующего прохода lifecycle это гонка, а не
# порча. Сотни означают, что консолидация db-узлов не доехала (20.08.2026
# таких было 3614).
_GRAPH_INTEGRITY_FAIL_STALE_DB_EDGES = 100


def check_graph_integrity(db: Session) -> CheckResult:
    """Структурные инварианты графа — regression-watch для багов, вычищенных
    в rc.2 (2026-06-26). Эти величины ДОЛЖНЫ быть 0 по построению:

      * db_dup_names_within_ns — `db:%`-узлы с одним именем в ОДНОМ namespace.
        Строго говоря, это проверка того, что констрейнт на месте:
        `UniqueConstraint("namespace", "name", "node_kind")` делает такой
        дубль невозможным на уровне БД. Счётчик оставлен именно поэтому —
        >0 означает, что констрейнт сняли миграцией, а не что синк
        сломался. Стоит он один GROUP BY.

        Раньше здесь считались узлы с одним именем в >1 namespace, и проверка
        держала fail постоянно: 16 имён, которые #185/#189 «не смогли
        добить». Убирать было нечего. Замер 20.08.2026: `db:postgres:message`
        существует в 56 namespace — по одному на окружение, и каждый узел
        собирает рёбра только своего: узел в `squad-1-shared` обслуживает
        `squad-1-*`, узел в `prod-shared` — `prod-*`. Это 56 разных физических
        баз, а не 56 копий одной. В мультитенантной инфраструктуре, где у
        каждого сквада полный набор БД, глобальная уникальность имени БД не
        инвариант, а ошибка модели.

      * live_edges_into_missing_ns_db — рёбра из ЖИВОГО namespace в `db:%`-узел
        namespace'а, помеченного `missing`. Это ложь о работающей системе:
        граф утверждает, что прод ходит в базу удалённого окружения.

        Замер 20.08.2026: 3614 таких рёбер, все в `preprod-kingdom1` —
        namespace, которого нет в кластере с 15.08. Происхождение известно:
        `phantom_db_cleanup` схлопывал копии в узел с лексикографически
        МИНИМАЛЬНЫМ namespace, и `preprod-kingdom1` оказался минимумом среди
        всех окружений. Фикс 15.08 (`contract.shared_namespace_of`) исправил
        going-forward — новые рёбра идут в `*-shared`, — но накопленные 5366
        остались на старом узле. Проверка их не видела, потому что искала
        совсем другое.

        Рёбра мёртвое→мёртвое сюда НЕ входят: у снесённых сквадов свои базы и
        свои сервисы, ~102 ребра внутри собственного окружения. Это мусор для
        retention в `namespace_lifecycle`, а не неверный факт.
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
        # Группировка по (name, namespace), а не по name: одно имя в разных
        # окружениях — норма, два узла с одним именем в одном namespace — нет.
        .group_by(Service.name, Service.namespace)
        .having(func.count(Service.id) > 1)
        .subquery()
    )
    db_dup_names_within_ns = db.query(func.count()).select_from(dup_sub).scalar() or 0

    # Рёбра из живого namespace в базу, лежащую в отсутствующем.
    src_s = aliased(Service)
    dst_s = aliased(Service)
    ns_src = aliased(Namespace)
    ns_dst = aliased(Namespace)
    live_edges_into_missing_ns_db = (
        db.query(func.count(ServiceEdge.id))
        .join(dst_s, ServiceEdge.dst_id == dst_s.id)
        .join(src_s, ServiceEdge.src_id == src_s.id)
        .join(ns_dst, ns_dst.namespace == dst_s.namespace)
        .join(ns_src, ns_src.namespace == src_s.namespace)
        .filter(dst_s.name.like("db:%"),
                ns_dst.state == "missing",
                ns_src.state == "active")
        .scalar()
    ) or 0

    # Рёбра `uses_db` в базу ЧУЖОГО окружения, у которого есть свой узел.
    #
    # Инвариант выше ловил только удалённые namespace, и этого оказалось
    # мало. Замер 21.08.2026, сразу после переноса 3740 рёбер: в графе
    # осталось 1900 кросс-окруженческих, из них у 1800 правильный узел в
    # своём окружении СУЩЕСТВОВАЛ. Двенадцать были прямой ложью о проде —
    # все семь `bot-service` из prod-kingdom1..7 указывали на
    # `preprod-kingdom2/db:postgres:map-coordinator`, при живом
    # `prod-shared/db:postgres:map-coordinator`. Живой получатель делал
    # ложь незаметной для проверки, которая смотрела на `state='missing'`.
    #
    # Условие «есть свой узел» здесь не педантизм: остальные 100 рёбер шли
    # из снесённого `squad-20-shared`, у которого db-узлов в графе нет ни
    # одного. Переносить их некуда, и держать проверку в fail из-за
    # намеченного к удалению сквада значит снова получить сигнал, на
    # который нельзя ответить.
    own_db = aliased(Service)
    cross_realm_db_edges = 0
    for (edge_id, src_ns, dst_ns, db_name, db_kind) in (
        db.query(ServiceEdge.id, src_s.namespace, dst_s.namespace,
                 dst_s.name, dst_s.node_kind)
        .join(dst_s, ServiceEdge.dst_id == dst_s.id)
        .join(src_s, ServiceEdge.src_id == src_s.id)
        .filter(dst_s.name.like("db:%"), ServiceEdge.kind == "uses_db")
        .all()
    ):
        src_realm = shared_namespace_of(src_ns)
        dst_realm = shared_namespace_of(dst_ns)
        if not src_realm or not dst_realm or src_realm == dst_realm:
            continue
        has_own = (
            db.query(own_db.id)
            .filter(own_db.name == db_name,
                    own_db.namespace == src_realm,
                    own_db.node_kind == db_kind)
            .first()
        )
        if has_own is not None:
            cross_realm_db_edges += 1

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

    stale_db_edges = live_edges_into_missing_ns_db + cross_realm_db_edges
    if (db_dup_names_within_ns > 0 or self_loops_any > 0
            or stale_db_edges > _GRAPH_INTEGRITY_FAIL_STALE_DB_EDGES
            or dangling_edges > _GRAPH_INTEGRITY_FAIL_DANGLING):
        status = "fail"
    elif dangling_edges > 0 or stale_db_edges > 0:
        status = "warn"
    else:
        status = "ok"

    return CheckResult(
        name="graph_integrity",
        status=status,
        detail={
            "db_dup_names_within_ns": db_dup_names_within_ns,
            "live_edges_into_missing_ns_db": live_edges_into_missing_ns_db,
            "cross_realm_db_edges": cross_realm_db_edges,
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
    check_edge_kind_freshness,
    check_deploy_stream_ingestion,
    check_graph_integrity,
    check_schema_version,
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
