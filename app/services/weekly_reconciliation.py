"""Еженедельная сверка «БД копилота против внешнего мира».

Self-health меряет копилот его же данными: синк ходит, heartbeat свежий,
таблица не пустая. На вопрос «а видит ли копилот то, что происходит в
кластере» он не отвечает — и не может, потому что смотрит внутрь.

День аудита 31.10.2026 нашёл четыре вещи, каждую из которых показала бы
одна строка этого отчёта в первую же неделю:

  * копилот видел 10 из 220 алертов группы — Alertmanager резал batch по
    `max_alerts`, а всё, что внутри, выглядело штатно;
  * kg_deployments раздулась до 1,24 млн строк без retention;
  * бэкап полгода не уезжал в S3;
  * медик сделал 173 прогона без единой подтверждённой починки.

Пять строк, у каждой число и пометка ок/плохо:

  1. покрытие алертов — пары (alertname, namespace) из ALERTS в
     VictoriaMetrics против kg_alerts за те же 7 дней;
  2. рост таблиц — топ-5 по приросту pg_total_relation_size за неделю;
  3. бэкап — возраст последнего успешного прогона CronJob `postgres-backup`;
  4. атрибуция — доля алертов без сервиса или на экспортере/synthetic;
  5. подтверждённые починки — verified-попытки executor-а + fixed у медика.

Каждый источник отвечает контрактом `SourceStatus`: недоступный источник
даёт строку «не измерено: <причина>», а не ноль и не падение отчёта. Ноль
здесь — утверждение о мире, и выдавать его за «не смогли спросить» нельзя.

БЕЗ LLM: чистая агрегация, как и `stats_digest`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast, Any, Dict, List, Optional, Set, Tuple

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.source_status import SourceStatus, mark

log = structlog.get_logger()

__all__ = [
    "ReconLine",
    "WINDOW_DAYS",
    "SIZE_SNAPSHOT_REDIS_KEY",
    "SIZE_SNAPSHOT_REDIS_TTL",
    "coverage_line",
    "table_growth_line",
    "backup_line",
    "attribution_line",
    "remediation_line",
    "format_report",
    "build_report",
    "run_weekly_reconciliation",
]

#: Окно сверки. Неделя — чтобы разовая тишина или разовый всплеск не решали.
WINDOW_DAYS = 7

# ── Пороги «плохо» ───────────────────────────────────────────────────────────
COVERAGE_MIN_PCT = 90.0
TABLE_GROWTH_MAX_PCT = 30.0
#: Рост в процентах судим только у таблиц, которые уже были не крошечными:
#: 8 КБ → 16 КБ — это +100%, но не повод для тревоги.
TABLE_GROWTH_MIN_BASE_BYTES = 10 * 1024 * 1024
DB_SIZE_MAX_BYTES = 5 * 1024 ** 3
BACKUP_MAX_AGE_HOURS = 26.0
UNATTRIBUTED_MAX_PCT = 30.0

#: Размеры прошлой недели. TTL 30 дней: пропущенный прогон не стирает базу,
#: но и сравнение с полуторамесячной давностью за «неделю» не выдаётся —
#: в тексте всегда подписано, сколько дней прошло.
SIZE_SNAPSHOT_REDIS_KEY = "stats:reconciliation:last"
SIZE_SNAPSHOT_REDIS_TTL = 30 * 24 * 3600

#: CronJob бэкапа копилота. Ключей S3 у API/воркера нет (они только в
#: секрете CronJob-а) и тащить их сюда незачем: kube-state-metrics знает время
#: последнего успешного прогона, а Job завершается успехом только после
#: `Backup complete` — upload в S3 стоит последним шагом скрипта.
BACKUP_NAMESPACE = "sre-ai"
BACKUP_CRONJOB = "postgres-backup"

#: severity=none по замыслу уходит в blackhole Alertmanager-а и до копилота
#: не доходит — исключаем с обеих сторон. info идёт в /store и входит.
_EXCLUDED_SEVERITY = "none"

#: Экспортеры метрик: их имя в `service` — ИСТОЧНИК метрики, а не объект
#: алерта. Совпадает с `alert_enrichment.METRIC_EXPORTER_SOURCES` + vmagent;
#: остальная обсервабилити помечена `kg_services.synthetic`.
_EXPORTER_SERVICES = (
    "vm-kube-state-metrics", "kube-state-metrics",
    "vm-node-exporter", "node-exporter", "prometheus-node-exporter",
    "vmagent-vm-victoria-metrics-k8s-stack", "vmagent",
)

_OK = "✅"
_BAD = "❌"
_UNMEASURED = "⚪"

#: Статусы источника, при которых число в строке есть (PARTIAL — с оговоркой).
_MEASURED = frozenset({SourceStatus.SUCCESS, SourceStatus.PARTIAL})


@dataclass
class ReconLine:
    """Одна строка отчёта.

    `bad` осмыслен только при измеренном статусе. `text` — сама строка
    без пометки; для неизмеренной — причина.
    """
    title: str
    status: SourceStatus
    text: str
    bad: bool = False
    value: Optional[float] = None

    @property
    def measured(self) -> bool:
        return self.status in _MEASURED

    def render(self) -> str:
        if not self.measured:
            return f"{_UNMEASURED} **{self.title}**: не измерено: {self.text}"
        marker = _BAD if self.bad else _OK
        return f"{marker} **{self.title}**: {self.text}"


def _unmeasured(title: str, status: SourceStatus, reason: str) -> ReconLine:
    return ReconLine(title=title, status=status, text=reason)


def _since(now: datetime) -> datetime:
    """Начало окна в формате колонок kg_* — naive UTC."""
    return (now - timedelta(days=WINDOW_DAYS)).astimezone(timezone.utc).replace(tzinfo=None)


def _raw_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fmt_bytes(n: float) -> str:
    for unit, size in (("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} Б"


# ── 1. Покрытие алертов ──────────────────────────────────────────────────────

_ALERTS_QUERY = (
    "group by (alertname, namespace) (max_over_time("
    f'ALERTS{{alertstate="firing",severity!="{_EXCLUDED_SEVERITY}"}}[{WINDOW_DAYS}d]))'
)


def _kg_alert_pairs(db: Session, since: datetime) -> Tuple[Set[Tuple[str, str]], Set[str]]:
    """Пары (alertname, namespace) из kg_alerts за окно.

    Алерт «был в окне», если он загорелся, был повторно прислан или
    закрылся внутри окна: хронический алерт с fired_at месячной давности
    Alertmanager шлёт повторно, и last_notified_at его в окно приводит.

    Namespace — у сервиса; у алерта без сервиса — `raw.namespace` (пишется
    с этого PR). Строки без сервиса, записанные раньше, namespace не хранят:
    их alertname возвращается отдельно. В покрытие они НЕ засчитываются —
    только показываются отдельным числом. Засчитать их «за любую пару с тем
    же именем» значило бы спрятать ровно то, ради чего строка заведена:
    первый живой замер 31.10.2026 — одна строка CPUThrottlingHigh без
    namespace «покрывала» 112 пар из ALERTS. raw перезаписывается каждым
    повторным уведомлением, так что за неделю после выката такие строки
    из окна уходят.
    """
    rows = db.execute(text("""
        SELECT a.alertname, s.namespace, a.raw
        FROM kg_alerts a
        LEFT JOIN kg_services s ON s.id = a.service_id
        WHERE (a.fired_at >= :since OR a.last_notified_at >= :since
               OR a.resolved_at >= :since)
          AND (a.severity IS NULL OR a.severity != :none)
    """), {"since": since, "none": _EXCLUDED_SEVERITY}).fetchall()
    pairs: Set[Tuple[str, str]] = set()
    nameless_ns: Set[str] = set()
    for alertname, ns, raw in rows:
        ns = ns or _raw_dict(raw).get("namespace")
        if ns:
            pairs.add((str(alertname), str(ns)))
        else:
            nameless_ns.add(str(alertname))
    return pairs, nameless_ns


async def coverage_line(db: Session, vm: Optional[VMClient], now: datetime) -> ReconLine:
    title = "Покрытие алертов"
    if vm is None:
        return _unmeasured(title, SourceStatus.UNAVAILABLE, "VICTORIA_METRICS_URL не задан")
    try:
        vm_pairs = await vm.query_label_sets_strict(_ALERTS_QUERY, ("alertname", "namespace"))
    except Exception as e:  # noqa: BLE001 — недоступный источник = строка, не падение
        return _unmeasured(title, SourceStatus.UNAVAILABLE, f"VictoriaMetrics: {type(e).__name__}")
    vm_pairs = {p for p in vm_pairs if p[0]}
    if not vm_pairs:
        return _unmeasured(
            title, SourceStatus.EMPTY,
            f"в ALERTS за {WINDOW_DAYS} дн. ни одной серии — источник молчит, а не кластер тих",
        )
    try:
        kg_pairs, nameless = _kg_alert_pairs(db, _since(now))
    except Exception as e:  # noqa: BLE001
        return _unmeasured(title, SourceStatus.FAILED, f"kg_alerts: {type(e).__name__}")
    covered = sum(1 for p in vm_pairs if p in kg_pairs)
    by_name_only = sum(1 for p in vm_pairs if p not in kg_pairs and p[0] in nameless)
    pct = 100.0 * covered / len(vm_pairs)
    note = (f"; ещё {by_name_only} совпали только по имени (строки без namespace)"
            if by_name_only else "")
    return ReconLine(
        title=title, status=SourceStatus.SUCCESS,
        text=(f"{covered} из {len(vm_pairs)} пар (alertname, namespace) "
              f"= {pct:.0f}% (порог {COVERAGE_MIN_PCT:.0f}%){note}"),
        bad=pct < COVERAGE_MIN_PCT, value=pct,
    )


# ── 2. Рост таблиц ───────────────────────────────────────────────────────────

def _read_table_sizes(db: Session) -> Tuple[int, Dict[str, int]]:
    """Размер БД и таблиц схемы public. Только PostgreSQL."""
    db_size = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    rows = db.execute(text("""
        SELECT c.relname, pg_total_relation_size(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND n.nspname = 'public'
    """)).fetchall()
    return int(cast(Any, db_size) or 0), {
        str(name): int(cast(Any, size) or 0) for name, size in rows
    }


def _redis():
    from app.services.digest.state import _get_beat_redis
    return _get_beat_redis()


def _load_size_snapshot(client: Any) -> Optional[Dict[str, Any]]:
    raw = client.get(SIZE_SNAPSHOT_REDIS_KEY)
    if not raw:
        return None
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def table_growth_line(db: Session, now: datetime, redis_client: Any = None) -> ReconLine:
    title = "Рост таблиц"
    if db.get_bind().dialect.name != "postgresql":
        return _unmeasured(title, SourceStatus.UNAVAILABLE, "размеры таблиц есть только в PostgreSQL")
    try:
        db_size, sizes = _read_table_sizes(db)
    except Exception as e:  # noqa: BLE001
        return _unmeasured(title, SourceStatus.FAILED, f"pg_total_relation_size: {type(e).__name__}")
    if not sizes:
        return _unmeasured(title, SourceStatus.EMPTY, "в схеме public нет таблиц")

    db_bad = db_size > DB_SIZE_MAX_BYTES
    db_part = f"БД {_fmt_bytes(db_size)} (порог {_fmt_bytes(DB_SIZE_MAX_BYTES)})"

    prev: Optional[Dict[str, Any]] = None
    redis_error: Optional[str] = None
    try:
        client = redis_client if redis_client is not None else _redis()
        prev = _load_size_snapshot(client)
        client.set(
            SIZE_SNAPSHOT_REDIS_KEY,
            json.dumps({"ts": now.isoformat(), "db": db_size, "tables": sizes}),
            ex=SIZE_SNAPSHOT_REDIS_TTL,
        )
    except Exception as e:  # noqa: BLE001 — Redis = канал наблюдаемости, fail-open
        redis_error = type(e).__name__
        log.warning("weekly_reconciliation.redis_failed", error=str(e))

    if redis_error is not None:
        # Размер БД измерен, рост — нет: строка есть, но с оговоркой.
        return ReconLine(
            title=title, status=SourceStatus.PARTIAL,
            text=f"{db_part}; рост не посчитан: Redis {redis_error}",
            bad=db_bad, value=float(db_size),
        )
    prev_tables = prev.get("tables") if prev else None
    if not isinstance(prev_tables, dict):
        return ReconLine(
            title=title, status=SourceStatus.SUCCESS,
            text=f"{db_part}; база заведена, рост покажет следующий прогон",
            bad=db_bad, value=float(db_size),
        )

    days = ""
    try:
        prev_ts = datetime.fromisoformat(str(prev.get("ts") if prev else ""))
        days = f" за {max((now - prev_ts).total_seconds() / 86400, 0):.0f} дн."
    except ValueError:
        pass

    growth: List[Tuple[str, int, Optional[float]]] = []
    bad_tables: List[str] = []
    for name, size in sizes.items():
        before = int(prev_tables.get(name) or 0)
        delta = size - before
        pct = 100.0 * delta / before if before else None
        growth.append((name, delta, pct))
        if pct is not None and before >= TABLE_GROWTH_MIN_BASE_BYTES and pct > TABLE_GROWTH_MAX_PCT:
            bad_tables.append(name)
    growth.sort(key=lambda g: g[1], reverse=True)
    top = [g for g in growth[:5] if g[1] > 0]
    if top:
        parts = []
        for name, delta, pct in top:
            pct_s = f" ({pct:+.0f}%)" if pct is not None else " (новая)"
            parts.append(f"`{name}` +{_fmt_bytes(delta)}{pct_s}")
        top_s = ", ".join(parts)
    else:
        top_s = "роста нет"
    return ReconLine(
        title=title, status=SourceStatus.SUCCESS,
        text=f"{db_part}; топ{days}: {top_s} (порог +{TABLE_GROWTH_MAX_PCT:.0f}%)",
        bad=db_bad or bool(bad_tables), value=float(db_size),
    )


# ── 3. Бэкап ─────────────────────────────────────────────────────────────────

_BACKUP_QUERY = (
    "max(kube_cronjob_status_last_successful_time"
    f'{{namespace="{BACKUP_NAMESPACE}",cronjob="{BACKUP_CRONJOB}"}})'
)


async def backup_line(vm: Optional[VMClient], now: datetime) -> ReconLine:
    title = "Бэкап в S3"
    if vm is None:
        return _unmeasured(title, SourceStatus.UNAVAILABLE, "VICTORIA_METRICS_URL не задан")
    try:
        sets = await vm.query_instant_by_labels_strict(_BACKUP_QUERY, ())
    except Exception as e:  # noqa: BLE001
        return _unmeasured(title, SourceStatus.UNAVAILABLE, f"VictoriaMetrics: {type(e).__name__}")
    last = next(iter(sets.values()), None)
    if not last:
        return _unmeasured(
            title, SourceStatus.EMPTY,
            f"нет kube_cronjob_status_last_successful_time для {BACKUP_NAMESPACE}/{BACKUP_CRONJOB}",
        )
    age_h = (now.timestamp() - last) / 3600
    return ReconLine(
        title=title, status=SourceStatus.SUCCESS,
        text=(f"последний успешный прогон {age_h:.1f} ч назад (порог {BACKUP_MAX_AGE_HOURS:.0f} ч); "
              "размер объекта без S3-ключей не меряем"),
        bad=age_h > BACKUP_MAX_AGE_HOURS, value=age_h,
    )


# ── 4. Атрибуция ─────────────────────────────────────────────────────────────

def attribution_line(db: Session, now: datetime) -> ReconLine:
    title = "Карточки без атрибуции"
    params: Dict[str, Any] = {
        "since": _since(now), "none": _EXCLUDED_SEVERITY,
        "exporters": list(_EXPORTER_SERVICES),
    }
    try:
        row = db.execute(text("""
            SELECT
              count(*) AS total,
              sum(CASE WHEN a.service_id IS NULL THEN 1 ELSE 0 END) AS no_service,
              sum(CASE WHEN a.service_id IS NOT NULL
                        AND (s.synthetic OR s.name IN :exporters)
                       THEN 1 ELSE 0 END) AS exporter
            FROM kg_alerts a
            LEFT JOIN kg_services s ON s.id = a.service_id
            WHERE (a.fired_at >= :since OR a.last_notified_at >= :since)
              AND (a.severity IS NULL OR a.severity != :none)
        """).bindparams(bindparam("exporters", expanding=True)), params).one()
    except Exception as e:  # noqa: BLE001
        return _unmeasured(title, SourceStatus.FAILED, f"kg_alerts: {type(e).__name__}")
    total = int(row[0] or 0)
    if total == 0:
        return _unmeasured(title, SourceStatus.EMPTY, f"в kg_alerts за {WINDOW_DAYS} дн. нет алертов")
    no_service, exporter = int(row[1] or 0), int(row[2] or 0)
    pct = 100.0 * (no_service + exporter) / total
    return ReconLine(
        title=title, status=SourceStatus.SUCCESS,
        text=(f"{no_service + exporter} из {total} = {pct:.0f}% "
              f"(без сервиса {no_service}, на экспортере/synthetic {exporter}; "
              f"порог {UNATTRIBUTED_MAX_PCT:.0f}%)"),
        bad=pct > UNATTRIBUTED_MAX_PCT, value=pct,
    )


# ── 5. Подтверждённые починки ────────────────────────────────────────────────

def remediation_line(db: Session, now: datetime) -> ReconLine:
    title = "Подтверждённые починки"
    since = _since(now)
    try:
        verified = int(db.execute(text("""
            SELECT count(*) FROM kg_remediation_attempts
            WHERE status = 'verified' AND updated_at >= :since
        """), {"since": since}).scalar() or 0)
        medic = db.execute(text("""
            SELECT count(*),
                   sum(CASE WHEN fixed THEN 1 ELSE 0 END)
            FROM kg_remediation_events
            WHERE started_at >= :since
        """), {"since": since}).one()
    except Exception as e:  # noqa: BLE001
        return _unmeasured(title, SourceStatus.FAILED, f"kg_remediation_*: {type(e).__name__}")
    runs, fixed = int(medic[0] or 0), int(medic[1] or 0)
    total = verified + fixed
    return ReconLine(
        title=title, status=SourceStatus.SUCCESS,
        text=(f"{total} (executor verified {verified}, медик fixed {fixed} "
              f"из {runs} прогонов)"),
        bad=total == 0, value=float(total),
    )


# ── Отчёт ────────────────────────────────────────────────────────────────────

def format_report(lines: List[ReconLine], now: datetime) -> str:
    """Заголовок с неделей + пять строк. ≤ 2000 символов (лимит content)."""
    start = (now - timedelta(days=WINDOW_DAYS)).date()
    iso = now.isocalendar()
    header = (f"🧾 Сверка копилота с кластером — неделя {iso[0]}-W{iso[1]:02d} "
              f"({start:%d.%m}–{now:%d.%m})")
    body = "\n".join(line.render() for line in lines)
    content = f"{header}\n{body}"
    if len(content) > 2000:
        content = content[:1999] + "…"
    return content


def _overall_status(lines: List[ReconLine]) -> SourceStatus:
    measured = sum(1 for line in lines if line.measured)
    if measured == len(lines):
        return SourceStatus.SUCCESS
    if measured:
        return SourceStatus.PARTIAL
    return SourceStatus.UNAVAILABLE


async def build_report(
    db: Session,
    *,
    vm: Optional[VMClient] = None,
    redis_client: Any = None,
    now: Optional[datetime] = None,
) -> Tuple[str, List[ReconLine]]:
    now = now or datetime.now(timezone.utc)
    lines = [
        await coverage_line(db, vm, now),
        table_growth_line(db, now, redis_client),
        await backup_line(vm, now),
        attribution_line(db, now),
        remediation_line(db, now),
    ]
    return format_report(lines, now), lines


async def run_weekly_reconciliation(db: Session) -> Dict[str, Any]:
    """Точка входа beat-задачи: собрать и отправить в канал статистики."""
    if not settings.WEEKLY_RECONCILIATION_ENABLED:
        return mark({"status": "skipped", "reason": "disabled"}, SourceStatus.UNAVAILABLE)
    vm = (VMClient(settings.VICTORIA_METRICS_URL, timeout=30.0)
          if settings.VICTORIA_METRICS_URL else None)
    content, lines = await build_report(db, vm=vm)
    from app.services.discord_service import discord_service
    delivered = await discord_service.send_stats_report(content)
    result: Dict[str, Any] = {
        "status": "sent" if delivered else "send_failed",
        "lines": {line.title: {"status": line.status.value, "bad": line.bad,
                               "value": line.value} for line in lines},
        "content_len": len(content),
    }
    log.info("weekly_reconciliation.done", **{"status": result["status"]})
    return mark(result, _overall_status(lines))
