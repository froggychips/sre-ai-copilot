"""Daily stats digest для Discord #stats канала.

ПОЛНОСТЬЮ data-aggregation. Не делает inference. Инвариант проверяется
тестом `tests/test_stats_digest_no_llm.py` — он грепает source на запрещённые
символы и завалится при попытке импорта reasoning-агентов.

Overhaul 2026-05-25: digest стал Δ-only + action-driven + observability-rich.
Изменения:
  * `_compute_change_report`/`_changes_section` — Δ vs Redis-snapshot prev-day.
  * Skip-if-noop в `send_daily_digest` — пустой digest вообще не постится.
  * `action_items_section` — chronic/unowned/suspicious_stale RCA-candidates.
  * `noisemakers_section` — top-3 сервисов генерирующих >20% алертов.
  * `mttr_section` — median/p95 MTTR resolved alerts за 7d + trend.
  * `deploy_incident_correlation_section` — deploy→alert within 30m.
  * `topology_growth_section` — Δ services/edges/NATS subjects vs snapshot.
  * `pipeline_health_section` — vmsingle/vmagent/AM/copilot/seq freshness.
  * `beat_heartbeats_footer` — last_run per sync task.
  * `recent_deploys_section` — clickable TC build URLs (Markdown link).

Секции (в порядке вывода, после overhaul):
  0. pipeline_health (header) — gauge vmagent/AM/copilot/seq freshness
  1. cluster_health — snapshot + Δ vs yesterday
  2. changes — Δ-only: new alerts / resolved / KG-edges (item A1)
  3. action_items — RCA-candidates (item B4)
  4. firing_alerts_by_squad
  5. unowned_namespaces
  6. top_alert_types — Δ24h + chronic/resurfaced
  7. noisemakers — top-3 сервисов с >20% alerts (item B5)
  8. mttr — resolved alerts last 7d (item B6)
  9. deploy_incident_correlation — deploy→alert within 30m (item B7)
  10. topology_growth — Δ services/edges/NATS (item B8)
  11. anomaly_summary / anomaly_top / log_errors (Wave 2)
  12. recent_deploys — TC deployer activity, clickable build URLs
  13. fragile_services / blast_radius
  14. stale_deployments
  15. kg_quality
  16. beat_heartbeats (footer, item C10)

Запускается через Celery beat task `daily_stats_digest`
(см. app/workers/tasks.py).
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient

log = structlog.get_logger()


# ── Overhaul 2026-05-25: shared dataclasses + Redis snapshot keys ───────────

# Все Redis ключи под prefix `stats:`. TTL не более 25h — переписывается каждым
# daily-run, при пропуске следующий run помечает (new baseline).
_DAY_SNAPSHOT_REDIS_KEY = "stats:digest:last_day_snapshot"
_TOPOLOGY_SNAPSHOT_REDIS_KEY = "stats:topology:last_day_snapshot"
_DAY_SNAPSHOT_REDIS_TTL = 25 * 3600

# Beat-task heartbeat ключи (см. _record_task_heartbeat / _get_beat_last_run).
# Пишутся `task_postrun`-сигналом из app/workers/tasks.py для каждого таска
# из `BEAT_HEARTBEAT_TASKS`. Используется pipeline_health_section чтобы
# отличать «task ходит, но данные stale» (VM scrape gap) от «task завис».
_BEAT_HEARTBEAT_REDIS_PREFIX = "stats:beat:last_run"
_BEAT_HEARTBEAT_REDIS_TTL = 7 * 24 * 3600  # 7 дней — на случай долгого простоя

# Ожидаемые интервалы (минуты) для тасков, проверяемых в pipeline_health.
# Совпадают с _SYNC_LAG_TARGETS в self_health, но дублируются здесь чтобы
# не тащить self_health-импорт в hot path heartbeat-проверки.
_BEAT_TASK_INTERVAL_MINUTES: Dict[str, int] = {
    "kg_topology_sync": 60,
    "kg_metrics_sync": 10,
    "kg_cluster_health_sync": 5,
    "kg_seq_logs_sync": 10,
    "kg_anomaly_detection_task": 10,
    "kg_signal_aggregates_compute": 60,  # компьютится hourly (window_end шагает /24h)
}


@dataclass
class ChangeReport:
    """Δ vs Redis-snapshot предыдущего дня. Item A1.

    Полее `new_baseline=True` если snapshot не нашёлся (первый запуск,
    Redis-flush, TTL истёк).
    """
    new_baseline: bool = False
    firing_series_today: int = 0
    firing_series_yesterday: Optional[int] = None
    crashloops_today: Optional[float] = None
    crashloops_yesterday: Optional[float] = None
    new_alerts_24h: int = 0
    resolved_alerts_24h: int = 0
    chronic_in_new: int = 0
    kg_edges_today: int = 0
    kg_edges_yesterday: Optional[int] = None
    kg_services_today: int = 0
    kg_services_yesterday: Optional[int] = None
    nats_subjects_new: List[str] = field(default_factory=list)


# TC web URL prefix — для clickable Markdown link в Recent deploys.
# Берём TEAMCITY_WEB_URL если задан, иначе fallback default.
_TC_URL_PREFIX_DEFAULT = "https://wo-teamcity.lastoasisgame.com"


def _tc_url_prefix() -> str:
    """Resolve URL prefix для TC build link. Settings → default."""
    return (
        getattr(settings, "TEAMCITY_WEB_URL", "")
        or getattr(settings, "TC_URL_PREFIX", "")
        or _TC_URL_PREFIX_DEFAULT
    ).rstrip("/")


def _get_ns_to_team_map(db: Session) -> Dict[str, str]:
    """Возвращает namespace → team_owner. Business-team приоритетнее `platform`.

    Один namespace может иметь несколько team_owner-ов (synthetic NATS-узлы
    помечаются `platform`, реальные сервисы — `kingdom1`/`shared`/etc.).
    Группируем `MIN()` с фильтром: предпочитаем не-`platform`.
    """
    rows = db.execute(text("""
        SELECT namespace, MIN(team_owner) AS team
        FROM kg_services
        WHERE team_owner IS NOT NULL AND team_owner != 'platform'
        GROUP BY namespace
    """)).fetchall()
    return {ns: team for ns, team in rows}


# Redis key для firing-series day-over-day trend (item #3 stats-UX).
# TTL 25h — чтобы переписывалось каждым daily-run, но не дольше суток без
# обновления (если digest упал — следующий run покажет «(new baseline)» при
# expired ключе).
_FIRING_SERIES_REDIS_KEY = "stats:firing_series:last_day"
_FIRING_SERIES_REDIS_TTL = 25 * 3600


async def _read_last_firing_series() -> Optional[int]:
    """Прочитать вчерашний firing-count из Redis. None если ключа нет."""
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        raw = await client.get(_FIRING_SERIES_REDIS_KEY)
        if raw is None:
            return None
        return int(raw)
    except Exception as e:
        log.warning("stats_digest.firing_series_redis_read_failed", error=str(e))
        return None


async def _write_last_firing_series(value: int) -> None:
    """Сохранить сегодняшний count для завтрашнего сравнения."""
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        await client.set(_FIRING_SERIES_REDIS_KEY, str(value), ex=_FIRING_SERIES_REDIS_TTL)
    except Exception as e:
        log.warning("stats_digest.firing_series_redis_write_failed", error=str(e))


# ── Snapshot helpers (item A1, B8) ──────────────────────────────────────────


async def _read_day_snapshot() -> Optional[Dict[str, Any]]:
    """Прочитать вчерашний snapshot. None если ключа нет / parse-error."""
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        raw = await client.get(_DAY_SNAPSHOT_REDIS_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("stats_digest.snapshot_read_failed", error=str(e))
        return None


async def _write_day_snapshot(snapshot: Dict[str, Any]) -> None:
    """Сохранить сегодняшний snapshot для завтрашнего Δ-сравнения."""
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        await client.set(
            _DAY_SNAPSHOT_REDIS_KEY,
            json.dumps(snapshot, default=str),
            ex=_DAY_SNAPSHOT_REDIS_TTL,
        )
    except Exception as e:
        log.warning("stats_digest.snapshot_write_failed", error=str(e))


async def _read_topology_snapshot() -> Optional[Dict[str, Any]]:
    """Прочитать topology snapshot. None если ключа нет."""
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        raw = await client.get(_TOPOLOGY_SNAPSHOT_REDIS_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("stats_digest.topology_snapshot_read_failed", error=str(e))
        return None


async def _write_topology_snapshot(snapshot: Dict[str, Any]) -> None:
    try:
        from app.services.alert_dedup import _get_client
        client = _get_client()
        await client.set(
            _TOPOLOGY_SNAPSHOT_REDIS_KEY,
            json.dumps(snapshot, default=str),
            ex=_DAY_SNAPSHOT_REDIS_TTL,
        )
    except Exception as e:
        log.warning("stats_digest.topology_snapshot_write_failed", error=str(e))


def _detect_first_run(snapshot: Optional[Dict[str, Any]]) -> bool:
    """First-run = ни snapshot нет, ни ключевых полей в нём.

    Используется topology_growth_section и потенциально другими first-run
    pill-эффектами. Вынесено в helper для симметрии и тестируемости.
    """
    if snapshot is None:
        return True
    # Snapshot есть, но пуст / dict без значимых полей — тоже first-run.
    if not snapshot:
        return True
    return False


def _beat_heartbeat_key(task_name: str) -> str:
    return f"{_BEAT_HEARTBEAT_REDIS_PREFIX}:{task_name}"


def _record_task_heartbeat(task_name: str, ts: Optional[datetime] = None) -> None:
    """Зафиксировать факт успешного завершения beat-task'а.

    Sync-функция, вызывается из celery `task_postrun`-сигнала. Использует
    synchronous redis client (не aioredis), потому что celery signal handler
    бежит в worker-процессе без event-loop.

    Идемпотентна: пишет ISO-timestamp в Redis с TTL 7d. Fail-open — при
    недоступности Redis warning в лог, exception не пробрасываем (это
    monitoring-канал, он не должен ломать сам task).
    """
    if ts is None:
        ts = datetime.now(timezone.utc)
    try:
        import redis
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.set(  # type: ignore[attr-defined]
            _beat_heartbeat_key(task_name),
            ts.isoformat(),
            ex=_BEAT_HEARTBEAT_REDIS_TTL,
        )
    except Exception as e:
        log.warning(
            "stats_digest.beat_heartbeat_write_failed",
            task=task_name,
            error=str(e),
        )


def _get_beat_last_run(task_name: str) -> Optional[datetime]:
    """Прочитать last_run timestamp beat-task'а.

    Sync — вызывается из sync pipeline_health_section. None если ключа нет
    (task ещё не отработал ни разу после переразвёртывания copilot'а).
    """
    try:
        import redis
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        raw = client.get(_beat_heartbeat_key(task_name))  # type: ignore[attr-defined]
        if raw is None:
            return None
        # decode_responses=True уже даёт str.
        return datetime.fromisoformat(raw)
    except Exception as e:
        log.warning(
            "stats_digest.beat_heartbeat_read_failed",
            task=task_name,
            error=str(e),
        )
        return None


def _fmt_firing_series_trend(today: int, yesterday: Optional[int]) -> str:
    """Формат trend-суффикса для `Firing series: 673 (+47 vs вчера, +7.5%)`.

    Если yesterday is None — это первый запуск, метим `(new baseline)`.
    Если разница 0 — `(=0 vs вчера)`.
    """
    if yesterday is None:
        return " (new baseline)"
    delta = today - yesterday
    if delta == 0:
        return " (=0 vs вчера)"
    sign = "+" if delta > 0 else ""
    if yesterday > 0:
        pct = (delta / yesterday) * 100
        return f" ({sign}{delta} vs вчера, {sign}{pct:.1f}%)"
    # yesterday=0, today>0 → не делим на ноль, просто +N
    return f" ({sign}{delta} vs вчера)"


def _fmt_delta_pp(today: Optional[float], yesterday: Optional[float]) -> str:
    """Δ в percentage-points: '+3pp' / '-1pp' / '±0pp'. None → пусто."""
    if today is None or yesterday is None:
        return ""
    delta = today - yesterday
    if abs(delta) < 0.5:
        return "±0pp"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}pp"


def _fmt_delta_int(today: Optional[float], yesterday: Optional[float]) -> str:
    """Δ для integer-метрик (crashloops): '+2' / '-1' / '±0'."""
    if today is None or yesterday is None:
        return ""
    delta = today - yesterday
    if abs(delta) < 0.5:
        return "±0"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}"


def _cluster_trend_24h(db: Session) -> Optional[Dict[str, Any]]:
    """Trend по kg_cluster_observations: today (24h) vs yesterday (24-48h).

    Возвращает None если:
      - таблица не существует (на dev без миграции);
      - в окне «вчера» < 1 sample (свежий rollout < 48h истории).
    Иначе dict с avg cpu/mem/crashloops и max disk_peak — сегодня + дельты.
    """
    try:
        today = db.execute(text("""
            SELECT avg(cpu_pct), avg(mem_pct), max(disk_peak_pct),
                   avg(crashloops), count(*)
            FROM kg_cluster_observations
            WHERE ts > NOW() - INTERVAL '24 hours'
        """)).fetchone()
        yesterday = db.execute(text("""
            SELECT avg(cpu_pct), avg(mem_pct), max(disk_peak_pct),
                   avg(crashloops), count(*)
            FROM kg_cluster_observations
            WHERE ts BETWEEN NOW() - INTERVAL '48 hours' AND NOW() - INTERVAL '24 hours'
        """)).fetchone()
    except Exception as e:
        log.warning("stats_digest.cluster_trend_missing_table", error=str(e))
        return None

    today_count = (today[4] if today else 0) or 0
    yesterday_count = (yesterday[4] if yesterday else 0) or 0
    if today_count == 0 or yesterday_count == 0:
        return None

    def _val(row, idx) -> Optional[float]:
        v = row[idx] if row else None
        return float(v) if v is not None else None

    return {
        "today_cpu": _val(today, 0),
        "today_mem": _val(today, 1),
        "today_disk": _val(today, 2),
        "today_crash": _val(today, 3),
        "yest_cpu": _val(yesterday, 0),
        "yest_mem": _val(yesterday, 1),
        "yest_disk": _val(yesterday, 2),
        "yest_crash": _val(yesterday, 3),
    }


async def cluster_health_section(
    vm: VMClient,
    fired_series: List[dict],
    db: Optional[Session] = None,
    *,
    firing_series_yesterday: Optional[int] = None,
) -> str:
    """1. Cluster health snapshot + trend vs yesterday.

    Snapshot — текущее состояние из VM (nodes_ready / crashloops).
    Trend — avg/max по kg_cluster_observations за сегодня (24h) vs вчера
    (24-48h). Если истории < 48h или таблицы нет — рисуем только snapshot
    с пометкой `_недостаточно данных для trend_`.

    `db` опционален для backwards-compat в тестах: если не передан, trend
    не считается.

    `firing_series_yesterday` — count из Redis-snapshot прошлого digest-run.
    Используется для дельты `Firing series: 673 (+47 vs вчера, +7.5%)`.
    None → метим `(new baseline)`.
    """
    try:
        ch = await vm.get_cluster_health()
        d = ch.to_dict() if hasattr(ch, "to_dict") else {}
        nodes = d.get("nodes_ready", "?")
        crash = d.get("crashloops", "?")
    except Exception as e:
        log.warning("stats_digest.cluster_health_failed", error=str(e))
        nodes, crash = "?", "?"

    firing_today = len(fired_series)
    firing_trend = _fmt_firing_series_trend(firing_today, firing_series_yesterday)
    snapshot_line = (
        f"  Nodes ready: `{nodes}` · Crashloops: `{crash}` · "
        f"Firing series: `{firing_today}`{firing_trend}"
    )

    trend = _cluster_trend_24h(db) if db is not None else None
    if trend is None:
        return "\n".join([
            "**🛡️ Cluster Health**",
            snapshot_line,
            "  _недостаточно данных для trend (нужно ≥48h истории)_",
        ])

    def _fmt_pct(today: Optional[float], yest: Optional[float], delta_str: str) -> str:
        t = f"{today:.0f}" if today is not None else "?"
        y = f"{yest:.0f}" if yest is not None else "?"
        suffix = f" ({delta_str})" if delta_str else ""
        return f"{y}→{t}%{suffix}"

    cpu_str = _fmt_pct(trend["today_cpu"], trend["yest_cpu"],
                       _fmt_delta_pp(trend["today_cpu"], trend["yest_cpu"]))
    mem_str = _fmt_pct(trend["today_mem"], trend["yest_mem"],
                       _fmt_delta_pp(trend["today_mem"], trend["yest_mem"]))
    disk_str = _fmt_pct(trend["today_disk"], trend["yest_disk"],
                        _fmt_delta_pp(trend["today_disk"], trend["yest_disk"]))
    crash_delta = _fmt_delta_int(trend["today_crash"], trend["yest_crash"])
    crash_y = f"{trend['yest_crash']:.0f}" if trend["yest_crash"] is not None else "?"
    crash_t = f"{trend['today_crash']:.0f}" if trend["today_crash"] is not None else "?"
    crash_suffix = f" ({crash_delta})" if crash_delta else ""

    trend_line = (
        f"  Trend (vs yesterday): cpu {cpu_str}, mem {mem_str}, "
        f"crashloops avg {crash_y}→{crash_t}{crash_suffix}, disk peak {disk_str}"
    )
    return "\n".join([
        "**🛡️ Cluster Health**",
        snapshot_line,
        trend_line,
    ])


def firing_alerts_section(
    fired_series: List[dict],
    ns_to_team: Dict[str, str],
) -> Tuple[str, Counter, defaultdict, defaultdict]:
    """2. Firing-series, grouped by squad (через KG namespace→team).

    Возвращает (rendered_text, unique_alertnames, team_alerts, unowned_ns_counts).
    `unowned_ns_counts` теперь возвращается отдельно — рендерится в собственной
    секции `unowned_namespaces_section` с suggested-owner эвристикой, а не
    inline-перечислением `monitoring=44, squad-7-shared=36, ...`.

    Юнит «series» (не русское «с», которое глаз читает как секунды).
    """
    ns_alerts: defaultdict = defaultdict(Counter)
    unique_alerts: Counter = Counter()
    for s in fired_series:
        m = s.get("metric", {})
        ns = m.get("namespace") or m.get("exported_namespace") or "(no-ns)"
        name = m.get("alertname", "?")
        ns_alerts[ns][name] += 1
        unique_alerts[name] += 1

    team_alerts: defaultdict = defaultdict(int)
    unowned: defaultdict = defaultdict(int)
    for ns, alerts in ns_alerts.items():
        team = ns_to_team.get(ns)
        total = sum(alerts.values())
        if team:
            team_alerts[team] += total
        else:
            unowned[ns] += total

    lines = ["**🚨 Firing alerts by squad** (последние 5 минут)"]
    if not team_alerts and not unowned:
        lines.append("  ✅ ни одной серии — кластер здоров")
    else:
        # Inline-формат «@team N series, ...» — single body line per teams.
        sorted_teams = sorted(team_alerts, key=lambda t: -team_alerts[t])
        if sorted_teams:
            parts = ", ".join(f"`@{t}` {team_alerts[t]} series" for t in sorted_teams)
            lines.append(f"  {parts}")
    return "\n".join(lines), unique_alerts, team_alerts, unowned


def unowned_namespaces_section(
    unowned: defaultdict,
    db: Optional[Session] = None,
    *,
    top_n: int = 5,
) -> str:
    """3. Unowned-namespaces — отдельной секцией с multi-signal suggest.

    Раньше unowned рисовался inline в firing_alerts (`monitoring=44, ...`),
    что не actionable. С 2026-05-24 переехало на
    `ownership_suggester.suggest_owner_multi_signal` (prefix + deploy-history +
    labels + manual manifest). Confidence отображается в формате `?` / `bold`:

      🔎 Unowned namespaces — нужны owner
        • monitoring         — 44 series · suggest: **`@platform`** (manual)
        • squad-7-kingdom2   — 36 series · suggest: **`@squad-7`**
        • prod-cdn           — 12 series · suggest: `@cdn`
        • weird-ns           — 8 series  · suggest: `@?-kemyashev` ?

    Правила рендера:
      - confidence ≥ 0.8 → **bold** suggestion (высокая уверенность).
      - confidence <  0.5 → суффикс ` ?` (слабая догадка).
      - sources содержит 'manual' → суффикс ` (manual)`.
      - owner is None → `?`.

    Если unowned пуст — секцию скрываем ("").
    """
    if not unowned:
        return ""

    from app.services.ownership_suggester import suggest_owner_multi_signal

    top = sorted(unowned.items(), key=lambda x: -x[1])[:top_n]

    lines = ["**🔎 Unowned namespaces** — нужны owner"]
    for ns, count in top:
        sug = suggest_owner_multi_signal(ns, db)
        if sug.owner is None:
            owner_str = "`?`"
        elif sug.owner == "multi-squad":
            # Bare `<env>-shared` намеренно подсвечивает «нужен manual», не врёт
            # конкретным squad-ом. Без bold (не high-confidence) и без `?`-суффикса
            # (это явный actionable маркер, а не догадка).
            owner_str = "`multi-squad` (shared, manual nudge)"
        else:
            base = f"`@{sug.owner}`"
            # Bold для high-confidence suggestion.
            if sug.confidence >= 0.8:
                base = f"**{base}**"
            owner_str = base
            # Manual marker — append.
            if sug.manual:
                owner_str = f"{owner_str} (manual)"
            # Low-confidence marker — append `?`.
            elif sug.confidence < 0.5:
                owner_str = f"{owner_str} ?"
        lines.append(f"  • `{ns}` — {count} series · suggest: {owner_str}")
    return "\n".join(lines)


# Noise-алерты Prometheus stack-а — не реальные проблемы, фильтруем
# из top-N чтобы не зашумлять digest. InfoInhibitor/Watchdog — служебные
# meta-alerts; CPUThrottlingHigh без severity критерия — часто false positive
# на bursty workload.
_NOISE_ALERTNAMES = frozenset({"InfoInhibitor", "Watchdog", "CPUThrottlingHigh"})


def _alert_type_metadata(
    db: Optional[Session],
    alertnames: List[str],
) -> Dict[str, Dict[str, Optional[int]]]:
    """Для каждого alertname посчитать (yesterday_cnt, chronic, resurfaced)
    из kg_alerts.

    Definitions:
      - `yesterday_cnt`: count series с этим alertname за 24-48h назад.
        **None** если за окно 24-48h в `kg_alerts` нет данных вообще
        (например, alert_state ещё не отслеживался ⇒ new baseline);
        **0** если данные есть, но не для этого alertname (legit «не fired»).
      - `chronic`: число сервисов где (service_id, alertname) имел ≥3 fires
        за последние 24h. None если 24h-окно пусто (нет tracking-а).
      - `resurfaced`: число сервисов где есть resolved_at И позже него ещё один
        fired_at в последних 24h. None если 24h-окно пусто.

    Если таблицы нет / db is None / запрос упал — возвращаем пустой dict.
    Caller рендерит без этих полей.
    """
    if db is None or not alertnames:
        return {}
    try:
        # Сначала проверяем наличие данных в окнах — нужно различать
        # «alert не fired вчера» (legit 0) и «вообще нет истории» (None /
        # new baseline). Если в kg_alerts за окно вообще 0 rows, отмечаем
        # `*_has_history=False`.
        yest_has_rows = db.execute(text("""
            SELECT EXISTS(
                SELECT 1 FROM kg_alerts
                WHERE fired_at BETWEEN NOW() - INTERVAL '48 hours'
                                   AND NOW() - INTERVAL '24 hours'
                LIMIT 1
            )
        """)).scalar()
        today_has_rows = db.execute(text("""
            SELECT EXISTS(
                SELECT 1 FROM kg_alerts
                WHERE fired_at > NOW() - INTERVAL '24 hours'
                LIMIT 1
            )
        """)).scalar()

        # 1) yesterday: за окно 24-48h назад, count(*) по alertname.
        yest_rows = db.execute(text("""
            SELECT alertname, count(*) AS cnt
            FROM kg_alerts
            WHERE alertname = ANY(:names)
              AND fired_at BETWEEN NOW() - INTERVAL '48 hours' AND NOW() - INTERVAL '24 hours'
            GROUP BY alertname
        """), {"names": alertnames}).fetchall()
        yesterday: Dict[str, int] = {name: cnt for name, cnt in yest_rows}

        # 2) chronic — service где (service_id, alertname) имеет ≥3 fires за 24h.
        chronic_rows = db.execute(text("""
            SELECT alertname, count(*) AS chronic_svc
            FROM (
                SELECT alertname, service_id, count(*) AS fires
                FROM kg_alerts
                WHERE alertname = ANY(:names)
                  AND fired_at > NOW() - INTERVAL '24 hours'
                  AND service_id IS NOT NULL
                GROUP BY alertname, service_id
                HAVING count(*) >= 3
            ) t
            GROUP BY alertname
        """), {"names": alertnames}).fetchall()
        chronic: Dict[str, int] = {name: cnt for name, cnt in chronic_rows}

        # 3) resurfaced — service-alertname пары где есть resolved + позднее fired.
        # Heuristic: max(resolved_at) < max(fired_at) при ≥2 fires.
        resurf_rows = db.execute(text("""
            SELECT alertname, count(*) AS resurf_svc
            FROM (
                SELECT alertname, service_id,
                       max(resolved_at) AS last_resolved,
                       max(fired_at) AS last_fired,
                       count(*) AS fires
                FROM kg_alerts
                WHERE alertname = ANY(:names)
                  AND fired_at > NOW() - INTERVAL '24 hours'
                  AND service_id IS NOT NULL
                GROUP BY alertname, service_id
                HAVING count(*) >= 2
                   AND max(resolved_at) IS NOT NULL
                   AND max(resolved_at) < max(fired_at)
            ) t
            GROUP BY alertname
        """), {"names": alertnames}).fetchall()
        resurfaced: Dict[str, int] = {name: cnt for name, cnt in resurf_rows}

    except Exception as e:
        log.warning("stats_digest.alert_type_metadata_failed", error=str(e))
        return {}

    out: Dict[str, Dict[str, Optional[int]]] = {}
    for name in alertnames:
        out[name] = {
            "yesterday": yesterday.get(name, 0) if yest_has_rows else None,
            "chronic": chronic.get(name, 0) if today_has_rows else None,
            "resurfaced": resurfaced.get(name, 0) if today_has_rows else None,
        }
    return out


def top_alert_types_section(
    unique_alerts: Counter,
    db: Optional[Session] = None,
) -> str:
    """4. Top-3 alertname по числу series, без infrastructure-noise.

    Каждая строка обогащается метаданными из kg_alerts:
      `KubeDeploymentReplicasMismatch × 75 (Δ24h +12, 23 chronic, 8 resurfaced)`

    Если db is None или таблицы нет — рендерим базовый формат без
    дополнительных полей (backwards-compat с тестами).
    """
    filtered = Counter({
        name: cnt for name, cnt in unique_alerts.items()
        if name not in _NOISE_ALERTNAMES
    })
    lines = ["**📋 Top alert types** (без infra-noise)"]
    if not filtered:
        lines.append("  _нет активных алертов_")
        return "\n".join(lines)

    top = filtered.most_common(3)
    top_names = [name for name, _ in top]
    metadata = _alert_type_metadata(db, top_names)

    for name, cnt in top:
        meta = metadata.get(name)
        if meta is None:
            # без db / запрос упал → базовый формат
            lines.append(f"  `{name}` × {cnt}")
            continue

        parts: List[str] = []
        yesterday = meta["yesterday"]
        chronic = meta["chronic"]
        resurfaced = meta["resurfaced"]

        # None у yesterday/chronic/resurfaced = «нет истории за окно». Раньше
        # мы превращали это в 0 и тихо рендерили `Δ24h +cnt` — что вводит в
        # заблуждение. Теперь явно проставляем `(new baseline)` маркер и не
        # показываем нулевые Δ-поля.
        if yesterday is None and chronic is None and resurfaced is None:
            parts.append("new baseline")
        else:
            if yesterday is None:
                parts.append("Δ24h ?")
            else:
                delta = cnt - yesterday
                sign = "+" if delta >= 0 else ""
                parts.append(f"Δ24h {sign}{delta}")
            if chronic:
                parts.append(f"{chronic} chronic")
            if resurfaced:
                parts.append(f"{resurfaced} resurfaced")

        suffix = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"  `{name}` × {cnt}{suffix}")
    return "\n".join(lines)


def _health_marker(score: float) -> str:
    """Цветовой маркер для health_score: 🟢 ≥0.7, 🟡 0.4-0.7, 🔴 <0.4."""
    if score >= 0.7:
        return "🟢"
    if score >= 0.4:
        return "🟡"
    return "🔴"


# Item #6 stats-UX: fragile vs blast-radius. «Fragile» — это активный сигнал
# деградации (health_score < threshold + ≥3 callers). «Blast-radius» — просто
# много inbound callers (риск, если что-то сломается, но сейчас всё ОК).
# Старая секция «Top fragile services» по inbound-callers без health_score —
# неправильно называла «fragile» то, что было просто «много callers».
_FRAGILE_HEALTH_THRESHOLD = 0.7
_FRAGILE_MIN_CALLERS = 3


def fragile_services_section(db: Session, ns_to_team: Dict[str, str]) -> str:
    """4. Top fragile / blast-radius services.

    Two paths:
      A) Если есть health_score для сервисов:
         - fragile = health_score < 0.7 AND inbound_callers ≥ 3 → секция
           «Top fragile services» (composite health_score из KG).
         - остальные (health либо хороший, либо нет, но callers ≥ 3) →
           «Top blast-radius services» (по inbound callers).
      B) Если health_score ни у кого не посчитан → только blast-radius
         (никаких inferred-fragile, чтобы не врать терминологией).

    Sort и cap = 3 на каждую секцию. Если обе пусты — рендерим plain blast-radius
    fallback с пометкой `нет edges`.
    """
    # Pull all candidate services с inbound-callers count + health_score.
    # SELECT в один проход; дальше классифицируем в Python.
    try:
        rows = db.execute(text("""
            SELECT s.name, s.namespace, s.health_score,
                   count(e.id) AS callers
            FROM kg_services s
            LEFT JOIN kg_service_edges e ON e.dst_id = s.id
            WHERE NOT s.synthetic
              AND (s.team_owner IS NULL OR s.team_owner != 'platform')
            GROUP BY s.id, s.name, s.namespace, s.health_score
            HAVING count(e.id) > 0
            ORDER BY callers DESC
        """)).fetchall()
    except Exception as e:
        log.warning("stats_digest.fragile_query_failed", error=str(e))
        rows = []

    fragile: List[Tuple[str, str, float, int]] = []
    blast: List[Tuple[str, str, Optional[float], int]] = []
    any_health = False
    for name, ns, score, callers in rows:
        if score is not None:
            any_health = True
            score_f = float(score)
            if score_f < _FRAGILE_HEALTH_THRESHOLD and callers >= _FRAGILE_MIN_CALLERS:
                fragile.append((name, ns, score_f, callers))
                continue
        blast.append((name, ns, float(score) if score is not None else None, callers))

    sections: List[str] = []

    if fragile:
        fragile.sort(key=lambda x: x[2])  # health_score asc (low=bad)
        lines = ["**⚠️ Top fragile services** (health_score < 0.7 + ≥3 callers)"]
        for name, ns, score_f, callers in fragile[:3]:
            team = ns_to_team.get(ns, "(unowned)")
            marker = _health_marker(score_f)
            lines.append(
                f"  {marker} `{name}` _{ns}_ — health `{score_f:.2f}` · "
                f"{callers} callers · @{team}"
            )
        sections.append("\n".join(lines))

    if blast:
        blast.sort(key=lambda x: -x[3])  # callers desc
        if any_health:
            header = "**🔗 Top blast-radius services** (по inbound callers)"
        else:
            header = "**🔗 Top blast-radius services** (inbound callers · health_score ещё не посчитан)"
        lines = [header]
        for name, ns, score_opt, callers in blast[:3]:
            team = ns_to_team.get(ns, "(unowned)")
            health_suffix = f" · health `{score_opt:.2f}`" if score_opt is not None else ""
            lines.append(f"  `{name}` _{ns}_ — {callers} callers{health_suffix} · @{team}")
        sections.append("\n".join(lines))

    if not sections:
        return "**🔗 Top blast-radius services** (по inbound callers)\n  _нет edges_"

    return "\n\n".join(sections)


# Expected-stale классификация (item #5 stats-UX). Backup/cron/system —
# нормально что они «не катились 60d»: это batch-инфраструктура, deploy редко.
# Их перенос в свёрнутую категорию убирает 80% шума из секции.
#
# Эвристика вынесена в `app/knowledge_graph/stale_classifier.py` (KG Coverage
# #4, 2026-05-24). Здесь re-export для legacy-импортов
# (`tests/test_stats_digest_ux.py` импортирует `stats_digest._classify_stale`
# по имени), плюс константы оставлены чтобы старые тесты на содержимое
# `_EXPECTED_STALE_NAMESPACES` не падали.
from app.knowledge_graph.contract import (  # noqa: E402
    STALE_CLASS_EXPECTED_STALE,
)
from app.knowledge_graph.schema import Service  # noqa: E402
# Re-exports для legacy-import паттерна (stats_digest._EXPECTED_STALE_*) —
# тесты и внешний код могут импортировать константы через stats_digest module.
from app.knowledge_graph.stale_classifier import (  # noqa: E402, F401
    _EXPECTED_STALE_NAME_INFIXES,
    _EXPECTED_STALE_NAME_SUFFIXES,
    _EXPECTED_STALE_NAMESPACES,
    _classify_stale,
)


def stale_deployments_section(
    db: Session,
    ns_to_team: Dict[str, str],
    threshold_days: int,
    *,
    kubectl_fn=None,
    hide_expected: Optional[bool] = None,
) -> str:
    """5. Deployments живые (replicas>0) но spec не апдейтился ≥ threshold_days.

    Compact-rendering: deployments с одинаковым name и одинаковым idle_days
    встречающиеся в 3+ namespace-ах рендерятся одной строкой
    (`town-db-backup × 5 kingdoms · 62d`).

    Backup/cron/system-deployments классифицируются как `expected` и
    скрываются (или показываются compact-pill-ом) — настраивается флагом
    `STATS_HIDE_EXPECTED_STALE` (default True). 80% шума в секции — это
    backup-deployments по 5 ns × 60d.

    Per-release deployer name через TC недостижим (TC устроен «buildtype per
    pipeline action», не «buildtype per helm-release»), поэтому not shown.
    Кто что катил отображается в `recent_deploys_section` отдельно.

    `kubectl_fn` — для DI в тестах.
    `hide_expected` — override settings.STATS_HIDE_EXPECTED_STALE для тестов.
    """
    fn = kubectl_fn or _kubectl_get_deployments_json
    if hide_expected is None:
        hide_expected = getattr(settings, "STATS_HIDE_EXPECTED_STALE", True)

    wo_namespaces = sorted({
        ns for (ns,) in db.execute(text("SELECT DISTINCT namespace FROM kg_services")).fetchall()
    })

    # KG Coverage #4: primary source of truth — `kg_services.stale_class`.
    # Если column заполнен (kg_sync уже прошёл) — фильтр expected через DB
    # вместо runtime `_classify_stale(name, ns)`. Legacy fallback нужен для
    # rows без column (старая инсталляция, ещё не было ни одного kg_sync) —
    # в этом случае возвращаемся к name/ns эвристике.
    #
    # Используем ORM query (не raw text) — это (а) тип-сейф через Service.*
    # колонки, (б) не ломает legacy MagicMock-тесты, где `db.execute(...)`
    # мокается для одного-единственного SELECT DISTINCT namespace.
    stale_class_map: Dict[Tuple[str, str], Optional[str]] = {}
    try:
        rows = (
            db.query(Service.namespace, Service.name, Service.stale_class)
            .filter(Service.stale_class.isnot(None))
            .all()
        )
        for ns_, name_, cls_ in rows:
            stale_class_map[(ns_, name_)] = cls_
    except Exception:  # pragma: no cover - defensive: MagicMock-тесты / стенды без миграции
        # Если column ещё нет (migration не накатана) или тесты передали
        # MagicMock — silently fallback на legacy `_classify_stale`.
        stale_class_map = {}

    def _is_expected(name: str, ns: str) -> bool:
        """primary: kg_services.stale_class; fallback: legacy эвристика."""
        col = stale_class_map.get((ns, name))
        if col is not None:
            return col == STALE_CLASS_EXPECTED_STALE
        return _classify_stale(name, ns) == "expected"

    now = datetime.now(timezone.utc)
    # entries: (idle, ns, name, team, replicas, last_update_dt)
    entries: List[Tuple[int, str, str, str, int, datetime]] = []
    expected_count = 0
    for ns in wo_namespaces:
        items = fn(ns)
        for dep in items:
            name = dep["metadata"]["name"]
            replicas = (dep.get("status") or {}).get("readyReplicas") or 0
            if replicas <= 0:
                continue
            last = _last_update(dep)
            if last is None:
                continue
            idle = (now - last).days
            if idle < threshold_days:
                continue
            # Item #5: expected stale (backup/system) → не в основной список.
            if hide_expected and _is_expected(name, ns):
                expected_count += 1
                continue
            team = ns_to_team.get(ns, "(unowned)")
            entries.append((idle, ns, name, team, replicas, last))

    lines = [f"**⏳ Stale deployments** (alive, не катились ≥{threshold_days}d)"]
    if not entries:
        if expected_count:
            lines.append(f"  ✅ ничего suspicious · скрыто `{expected_count}` expected (backup/system)")
        else:
            lines.append("  ✅ ничего не stale")
        return "\n".join(lines)

    # Group by (name, idle_days) — деплоится по всем kingdom-ам синхронно,
    # это типичный case: 5 одинаковых backup-deployments × 62d.
    by_group: defaultdict = defaultdict(list)
    for e in entries:
        key = (e[2], e[0])  # (name, idle_days)
        by_group[key].append(e)

    rendered_groups: List[Tuple[int, str]] = []  # (max_idle для sort, rendered_line)
    seen_namespaces: set = set()
    for (name, idle), group in by_group.items():
        if len(group) >= 3:
            namespaces = sorted({e[1] for e in group})
            teams = sorted({e[3] for e in group})
            seen_namespaces.update(namespaces)
            teams_str = ",".join(f"@{t}" for t in teams[:3])
            if len(teams) > 3:
                teams_str += f"+{len(teams)-3}"
            rendered_groups.append((
                idle,
                f"  • `{name}` × {len(group)} ns ({teams_str}) · idle **{idle}d**",
            ))

    singular: List[Tuple[int, str, str, str, int, datetime]] = sorted(
        (e for e in entries if e[1] not in seen_namespaces),
        key=lambda e: (-e[0], e[2]),
    )
    singular_cap = 5
    for e in singular[:singular_cap]:
        idle, ns, name, team, _r, _last_dt = e
        rendered_groups.append((
            idle,
            f"  • `{name}` _{ns}_ (@{team}) · idle **{idle}d**",
        ))

    rendered_groups.sort(key=lambda x: -x[0])
    cap_total = 6
    for _idle, line in rendered_groups[:cap_total]:
        lines.append(line)
    # Строка «… и ещё N (скрыто)» убрана: счётчик не actionable, занимает
    # место, провоцирует FOMO. Если хвост важен — в digest всё равно влезает
    # с cap_total=6 + threshold ≥30d.
    #
    # Expected stale (backup/cron/system) показываем как итоговую pill —
    # «всё ок, не application-deploys» — на одну строку.
    if expected_count:
        lines.append(f"  _expected (backup/system): скрыто `{expected_count}`_")
    return "\n".join(lines)


# ── recent TC deploys (cluster-wide deployer activity) ──────────────────────


def _humanize_ago(iso_str: Optional[str], now: Optional[datetime] = None) -> str:
    """ISO timestamp → '5m ago' / '2h ago' / '3d ago'."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _cascade_fingerprint(build: dict) -> Tuple[Any, ...]:
    """Fingerprint для агрегации cascade-deploys.

    Один TC build (build chain triggered одним `Build and update` action)
    может выкатить 3+ сервиса в одном kingdom — TC создаёт N отдельных
    билдов с **одинаковым** `(number, branch, triggered_by, status)`. Без
    группировки они засоряют embed по 3 строки на одно событие.

    Намеренно НЕ включаем `id` (он у каждого билда свой) и НЕ финализуем
    по `finished_at` (cascade-builds стартуют параллельно и финишируют в
    разное время — sub-минутные расхождения).
    """
    return (
        build.get("number"),
        (build.get("branch") or "").replace("refs/heads/", ""),
        build.get("triggered_by") or build.get("triggered_type"),
        build.get("status"),
    )


def _format_services_list(names: List[str], cap: int = 3) -> str:
    """`['town-service', 'chat-tasks-service', 'map-service', 'a', 'b']`
    → `'town-service/chat-tasks-service/map-service +2 more'`."""
    if not names:
        return "?"
    shown = names[:cap]
    rest = len(names) - cap
    base = "/".join(shown)
    if rest > 0:
        base += f" +{rest} more"
    return base


async def recent_deploys_section(
    *,
    lookback_hours: int = 24,
    limit: int = 5,
    fetch_fn=None,
) -> str:
    """6. Cluster-wide TC deployer activity за последние N часов.

    Per-release deployer не работает (см. stale_deployments_section), но
    «кто что катил» — рабочий запрос: top-N finished deploy-builds, sorted
    by finished_at. Показывает trigger-user (или type для auto-triggered).

    2026-05-24 preview-fix: cascade-deploys (один TC «Build and update»
    разваливает на N parallel deploy-builds для разных сервисов в одном ns)
    агрегируются по fingerprint `(number, branch, triggered_by, status)`.
    Один build = одна логическая запись с `svc1/svc2/svc3 +N more`.

    Header:
      - есть результаты за 24h → `(24h)`;
      - 24h пусто → fallback на 7d, header → `(last 7d, 24h тихо)`;
      - 7d тоже пусто → секция скрыта.

    `fetch_fn` — для DI в тестах. По умолчанию `teamcity_service.recent_deploys`.
    """
    if fetch_fn is None:
        from app.services.teamcity_service import recent_deploys as _rd
        fetch_fn = _rd
    try:
        builds = await fetch_fn(lookback_hours=lookback_hours, limit=limit * 4)
    except Exception as e:
        log.warning("stats_digest.recent_deploys_failed", error=str(e))
        return ""

    header_suffix = f"({lookback_hours}h)"
    # Fallback: если за 24h пусто (тихий день), попробуем 7d-окно и пометим
    # header чтобы зритель понимал «секция не баг — просто 24h спокойно».
    if not builds and lookback_hours <= 24:
        try:
            builds = await fetch_fn(lookback_hours=24 * 7, limit=limit * 4)
        except Exception as e:
            log.warning("stats_digest.recent_deploys_fallback_failed", error=str(e))
            return ""
        if builds:
            header_suffix = "(last 7d, 24h тихо)"

    if not builds:
        return ""

    # Aggregate cascade-deploys по fingerprint, сохраняя порядок (newest first).
    groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for b in builds:
        fp = _cascade_fingerprint(b)
        if fp in groups:
            groups[fp]["builds"].append(b)
        else:
            groups[fp] = {"first": b, "builds": [b]}

    lines = [f"**🔧 Recent deploys** {header_suffix}"]

    now = datetime.now(timezone.utc)
    for fp, grp in list(groups.items())[:limit]:
        first = grp["first"]
        group_builds = grp["builds"]

        user = first.get("triggered_by")
        trig_type = first.get("triggered_type") or "?"
        actor = f"`{user}`" if user else f"_{trig_type}_"
        branch = (first.get("branch") or "?").replace("refs/heads/", "")
        num = first.get("number") or "?"
        status = first.get("status") or "?"
        ago = _humanize_ago(first.get("finished_at"), now)
        status_marker = "" if status == "SUCCESS" else f" · ⚠️ {status}"

        # Overhaul: build_id → clickable Markdown link.
        # Pre-existing field `url` берём приоритетно (заполняет teamcity_service),
        # fallback на построение из TC_URL_PREFIX + build.id.
        build_id = first.get("id")
        build_url = first.get("url")
        if not build_url and build_id:
            build_url = f"{_tc_url_prefix()}/viewLog.html?buildId={build_id}"
        # TODO: если build_id нет (некоторые fixtures, MCP fallback) — оставляем
        # plain text. Не сыпем 404-ссылки.

        if len(group_builds) == 1:
            # Single-build (нет cascade) — старый формат, чтобы не ломать
            # backwards-compat с existing snapshot-тестами.
            btype = first.get("buildtype_name") or "?"
            label = f"`{btype}` ({branch} #{num})"
            if build_url:
                # Markdown-link только вокруг buildtype+number, чтобы fmt не ломал
                # парсер ` (… #N)`.
                label = f"[{btype} ({branch} #{num})]({build_url})"
            lines.append(
                f"  • by {actor} · {label} · {ago}{status_marker}"
            )
        else:
            # Cascade aggregation: список service-имён сжимается до svc1/svc2/svc3 +N.
            service_names = [b.get("buildtype_name") or "?" for b in group_builds]
            services_str = _format_services_list(service_names)
            head = f"#{num} by {actor}"
            if build_url:
                head = f"[#{num} by {actor}]({build_url})"
            lines.append(
                f"  • {head} · {ago} · `{services_str}` @ {branch}{status_marker}"
            )
    return "\n".join(lines)


# ── anomaly_summary / anomaly_top / log_errors (Wave 2) ────────────────────


# Маппинг сырых имён метрик в короткие подписи для inline-перечисления.
# Если метрика новая (нет в маппинге) — показываем как есть.
_METRIC_LABELS = {
    "cpu_pct": "cpu",
    "mem_pct": "mem",
    "restarts_rate": "restarts",
    "http_5xx_rate": "5xx",
    "p95_latency_ms": "p95",
}


def _metrics_sync_lag_minutes(db: Session) -> Optional[float]:
    """DQ polish 2026-05-25: возвращает lag в минутах для kg_metrics_sync.

    Источник — self_health.check_sync_lag (тот же что и pipeline_health).
    Возвращает None если data недоступна (check упал, task не зарегистрирован,
    last_ts отсутствует). Любая ошибка swallowed — это диагностика, не
    блокирующий путь.
    """
    try:
        from app.knowledge_graph.self_health import check_sync_lag
        result = check_sync_lag(db)
        per_task = result.detail.get("per_task", {})
        info = per_task.get("kg_metrics_sync")
        if info is None:
            return None
        lag = info.get("lag_minutes")
        if lag is None:
            return None
        return float(lag)
    except Exception as e:
        log.warning("stats_digest.metrics_sync_lag_failed", error=str(e))
        return None


def anomaly_summary_section(db: Session) -> str:
    """5. Summary anomaly за 24h из kg_anomaly_observations.

    Total + breakdown by severity + by metric + top-5 affected services.
    Если таблицы нет в БД (на dev без миграции) — возвращаем "" (секция
    не показывается). Если таблица есть но 0 anomaly — рисуем «всё в норме».

    DQ polish 2026-05-25: если kg_metrics_sync отстаёт > threshold —
    рисуем degraded-режим (warning baseline counts могут быть stale).
    Если отстаёт > 4× threshold — скрываем секцию совсем кроме одной
    строки-стикера.
    """
    # Проверяем metrics-sync lag сразу — это контролирует degrade mode.
    threshold = getattr(settings, "ANOMALY_STALE_THRESHOLD_MINUTES", 60)
    lag_min = _metrics_sync_lag_minutes(db)
    severe_threshold = threshold * 4

    if lag_min is not None and lag_min > severe_threshold:
        # Слишком stale — секция не информативна, ставим одну строку.
        hours = lag_min / 60
        return (
            f"**📈 Anomalies**: skipped (metrics sync stale {hours:.1f}h+)"
        )

    try:
        total_row = db.execute(text("""
            SELECT count(*), count(DISTINCT service_id)
            FROM kg_anomaly_observations
            WHERE ts > NOW() - INTERVAL '24 hours'
        """)).fetchone()
    except Exception as e:
        log.warning("stats_digest.anomaly_summary_missing_table", error=str(e))
        return ""

    total = (total_row[0] if total_row else 0) or 0
    distinct_services = (total_row[1] if total_row else 0) or 0

    # DQ polish 2026-05-25: degraded-header при stale metrics sync.
    degraded_header_suffix = ""
    degraded_note = ""
    if lag_min is not None and lag_min > threshold:
        hours = lag_min / 60
        degraded_header_suffix = f" ⚠️ stale: metrics sync {hours:.1f}h ago"
        degraded_note = "  _Counts могут не отражать current state._"

    if total == 0:
        body = (
            f"**📈 Anomalies (last 24h){degraded_header_suffix}**\n"
            "  ✅ ни одной аномалии — поведение в норме"
        )
        if degraded_note:
            body += "\n" + degraded_note
        return body

    by_severity = {
        sev: cnt for sev, cnt in db.execute(text("""
            SELECT severity, count(*)
            FROM kg_anomaly_observations
            WHERE ts > NOW() - INTERVAL '24 hours'
            GROUP BY severity
        """)).fetchall()
    }
    warning_cnt = by_severity.get("warning", 0)
    critical_cnt = by_severity.get("critical", 0)

    top_services = db.execute(text("""
        SELECT s.name, count(a.id) AS cnt
        FROM kg_anomaly_observations a
        JOIN kg_services s ON s.id = a.service_id
        WHERE a.ts > NOW() - INTERVAL '24 hours'
        GROUP BY s.id, s.name
        ORDER BY cnt DESC
        LIMIT 5
    """)).fetchall()

    by_metric = db.execute(text("""
        SELECT metric, count(*)
        FROM kg_anomaly_observations
        WHERE ts > NOW() - INTERVAL '24 hours'
        GROUP BY metric
        ORDER BY count(*) DESC
    """)).fetchall()

    lines = [f"**📈 Anomalies (last 24h){degraded_header_suffix}**"]
    if degraded_note:
        lines.append(degraded_note)
    lines.append(
        f"  Total: {total} across {distinct_services} svc "
        f"(warning: {warning_cnt}, critical: {critical_cnt})"
    )
    if top_services:
        top_parts = ", ".join(f"`{name}` ×{cnt}" for name, cnt in top_services)
        lines.append(f"  Top affected: {top_parts}")
    if by_metric:
        metric_parts = ", ".join(
            f"{_METRIC_LABELS.get(m, m)}×{c}" for m, c in by_metric
        )
        lines.append(f"  By metric: {metric_parts}")
    return "\n".join(lines)


def anomaly_top_section(db: Session, ns_to_team: Dict[str, str]) -> str:
    """11a. Persistent anomalies за 7d: top-3 (service, metric) пар.

    «Persistent» = метрика на сервисе ловит anomaly ≥3 раз за неделю. Сюда
    подмешиваем самый свежий z_score чтобы реciever видел не только «было
    плохо», а текущий «насколько плохо сейчас». Если таблицы нет или 0
    persistent-кейсов — возвращаем "" (секция скрыта).
    """
    try:
        rows = db.execute(text("""
            WITH ranked AS (
                SELECT
                    a.service_id,
                    a.metric,
                    a.z_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.service_id, a.metric
                        ORDER BY a.ts DESC
                    ) AS rn
                FROM kg_anomaly_observations a
                WHERE a.ts > NOW() - INTERVAL '7 days'
            )
            SELECT s.name, s.namespace, r.metric,
                   count(*) AS events,
                   max(CASE WHEN r.rn = 1 THEN r.z_score END) AS latest_z
            FROM ranked r
            JOIN kg_services s ON s.id = r.service_id
            GROUP BY s.id, s.name, s.namespace, r.metric
            HAVING count(*) > 0
            ORDER BY events DESC, latest_z DESC NULLS LAST
            LIMIT 3
        """)).fetchall()
    except Exception as e:
        log.warning("stats_digest.anomaly_top_missing_table", error=str(e))
        return ""

    if not rows:
        return ""

    lines = ["**🔬 Persistent anomalies (last 7d)**"]
    for name, ns, metric, events, latest_z in rows:
        team = ns_to_team.get(ns, "(unowned)")
        metric_label = _METRIC_LABELS.get(metric, metric)
        z_str = f"z={float(latest_z):.1f}" if latest_z is not None else "z=?"
        lines.append(
            f"  `{name}` {metric_label} — {events} events, latest {z_str} · @{team}"
        )
    return "\n".join(lines)


def log_errors_section(db: Session, ns_to_team: Dict[str, str]) -> str:
    """11b. Top-3 service по error/fatal log-count за 24h.

    Источник — kg_log_observations (Seq-aggregator beat). Если таблицы нет
    или пуста (SEQ env-vars не сконфигурены) — возвращаем "". Sample-сообщение
    обрезаем до 60 chars чтобы строка не уехала за ширину embed-а.
    """
    try:
        rows = db.execute(text("""
            SELECT s.name, s.namespace,
                   SUM(l.count)::int AS total,
                   MAX(l.sample_message) AS sample
            FROM kg_log_observations l
            JOIN kg_services s ON s.id = l.service_id
            WHERE l.ts > NOW() - INTERVAL '24 hours'
              AND l.level IN ('Error', 'Fatal')
            GROUP BY s.id, s.name, s.namespace
            HAVING SUM(l.count) > 0
            ORDER BY total DESC
            LIMIT 3
        """)).fetchall()
    except Exception as e:
        log.warning("stats_digest.log_errors_missing_table", error=str(e))
        return ""

    if not rows:
        return ""

    lines = ["**📜 Log errors (last 24h)**"]
    for name, ns, total, sample in rows:
        team = ns_to_team.get(ns, "(unowned)")
        sample_str = (sample or "").strip().replace("\n", " ")
        if len(sample_str) > 60:
            sample_str = sample_str[:57] + "..."
        sample_suffix = f" (sample: {sample_str})" if sample_str else ""
        lines.append(
            f"  `{name}` — {total} errors{sample_suffix} · @{team}"
        )
    return "\n".join(lines)


def kg_quality_section(db: Session) -> str:
    """6. KG quality: services, orphan%, edges, team_owner coverage.

    Формальное определение orphan / synthetic / threshold-ов — см.
    `app.knowledge_graph.contract` (KG_SCHEMA_VERSION, QUALITY_THRESHOLDS,
    is_orphan, is_synthetic) и `docs/KG_SCHEMA_CONTRACT.md`. Логика
    ниже — оптимизированная SQL-агрегация, эквивалентная сумме per-service
    `is_orphan(s, edges)` из contract.py.
    """
    services_total = db.execute(text("SELECT count(*) FROM kg_services")).scalar() or 0
    if services_total == 0:
        return "**🧬 KG quality**\n  _KG пустой — kg_topology_sync ещё не выполнялся_"

    edges_total = db.execute(
        text("SELECT count(*) FROM kg_service_edges")
    ).scalar() or 0
    edges_by_kind: Dict[str, int] = {
        k: v for k, v in db.execute(
            text("SELECT kind, count(*) FROM kg_service_edges GROUP BY kind")
        ).fetchall()
    }
    synthetic = db.execute(
        text("SELECT count(*) FROM kg_services WHERE synthetic = true")
    ).scalar() or 0
    # Orphan = real services без edges. Synthetic (backup-cron'ы, nats-tools,
    # observability-exporters) исключены: они никогда edges не имеют по дизайну
    # и засчитывались бы как ложно-orphan.
    orphan = db.execute(text("""
        SELECT count(*) FROM kg_services s
        WHERE NOT s.synthetic
          AND s.id NOT IN (
              SELECT src_id FROM kg_service_edges
              UNION SELECT dst_id FROM kg_service_edges
          )
    """)).scalar() or 0
    real_total = services_total - synthetic
    pct_orphan = (100 * orphan // real_total) if real_total else 0
    edges_str = ", ".join(f"{k}={v}" for k, v in sorted(edges_by_kind.items()))
    synthetic_suffix = f" · synthetic скрыты: `{synthetic}`" if synthetic else ""

    return "\n".join([
        "**🧬 KG quality**",
        f"  Services: `{services_total}` · Orphan: `{orphan}`/`{real_total}` ({pct_orphan}%){synthetic_suffix}",
        f"  Edges: `{edges_total}` ({edges_str})",
    ])


def _kubectl_get_deployments_json(namespace: str) -> List[Dict[str, Any]]:
    """Helper для stale-deployments. Изолирован чтобы мокать в тестах."""
    try:
        out = subprocess.run(
            ["kubectl", "get", "deployments", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return []
        return json.loads(out.stdout).get("items", [])
    except Exception as e:
        log.warning("stats_digest.kubectl_failed", ns=namespace, error=str(e))
        return []


def _last_update(deployment: Dict[str, Any]) -> Optional[datetime]:
    """Max lastUpdateTime среди conditions; fallback на creationTimestamp."""
    last: Optional[datetime] = None
    for cond in (deployment.get("status", {}).get("conditions") or []):
        t = cond.get("lastUpdateTime")
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if last is None or dt > last:
                last = dt
        except ValueError:
            pass
    if last is None:
        t = (deployment.get("metadata") or {}).get("creationTimestamp")
        if t:
            try:
                last = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                pass
    return last


# ── Overhaul sections (item A1, A2, B4-B8, C9-C10) ─────────────────────────


def _count_alerts_in_window(db: Session, hours: int) -> Tuple[int, int]:
    """Возвращает (fired_in_window, resolved_in_window).

    fired_in_window: kg_alerts.fired_at в окне `hours`.
    resolved_in_window: kg_alerts.resolved_at в окне `hours`.

    На ошибке (нет таблицы) → (0, 0).
    """
    try:
        fired = db.execute(text("""
            SELECT count(*) FROM kg_alerts
            WHERE fired_at > NOW() - (:hours || ' hours')::interval
        """), {"hours": str(hours)}).scalar() or 0
        resolved = db.execute(text("""
            SELECT count(*) FROM kg_alerts
            WHERE resolved_at IS NOT NULL
              AND resolved_at > NOW() - (:hours || ' hours')::interval
        """), {"hours": str(hours)}).scalar() or 0
        return int(fired), int(resolved)
    except Exception as e:
        log.warning("stats_digest.count_alerts_failed", error=str(e))
        return 0, 0


def _count_chronic_in_window(db: Session, hours: int, min_fires: int = 5) -> int:
    """Сколько (service, alertname) пар с ≥ min_fires fires в окне."""
    try:
        cnt = db.execute(text("""
            SELECT count(*) FROM (
                SELECT service_id, alertname, count(*) AS fires
                FROM kg_alerts
                WHERE fired_at > NOW() - (:hours || ' hours')::interval
                  AND service_id IS NOT NULL
                GROUP BY service_id, alertname
                HAVING count(*) >= :min_fires
            ) t
        """), {"hours": str(hours), "min_fires": min_fires}).scalar() or 0
        return int(cnt)
    except Exception as e:
        log.warning("stats_digest.chronic_count_failed", error=str(e))
        return 0


def _count_edges(db: Session) -> int:
    try:
        return int(db.execute(text("SELECT count(*) FROM kg_service_edges")).scalar() or 0)
    except Exception:
        return 0


def _count_real_services(db: Session) -> int:
    try:
        return int(db.execute(text(
            "SELECT count(*) FROM kg_services WHERE NOT synthetic"
        )).scalar() or 0)
    except Exception:
        return 0


def _nats_subjects(db: Session) -> List[str]:
    """Список distinct NATS-subjects из kg_service_edges (kind='uses_nats').

    На отсутствие таблицы / kind → пустой список. Используется в topology
    growth snapshot.
    """
    try:
        rows = db.execute(text("""
            SELECT DISTINCT extras->>'subject' AS subj
            FROM kg_service_edges
            WHERE kind = 'uses_nats'
              AND extras IS NOT NULL
              AND extras->>'subject' IS NOT NULL
        """)).fetchall()
        return sorted({r[0] for r in rows if r[0]})
    except Exception:
        return []


async def _compute_change_report(
    db: Session,
    firing_today: int,
    crashloops_today: Optional[float],
    previous: Optional[Dict[str, Any]],
) -> ChangeReport:
    """Собрать ChangeReport vs `previous` snapshot.

    `previous=None` → new_baseline=True, deltas пусты.
    """
    new_alerts, resolved_alerts = _count_alerts_in_window(db, hours=24)
    chronic = _count_chronic_in_window(db, hours=24, min_fires=5)
    edges_today = _count_edges(db)
    services_today = _count_real_services(db)

    if previous is None:
        return ChangeReport(
            new_baseline=True,
            firing_series_today=firing_today,
            crashloops_today=crashloops_today,
            new_alerts_24h=new_alerts,
            resolved_alerts_24h=resolved_alerts,
            chronic_in_new=chronic,
            kg_edges_today=edges_today,
            kg_services_today=services_today,
        )

    return ChangeReport(
        new_baseline=False,
        firing_series_today=firing_today,
        firing_series_yesterday=previous.get("firing_series"),
        crashloops_today=crashloops_today,
        crashloops_yesterday=previous.get("crashloops"),
        new_alerts_24h=new_alerts,
        resolved_alerts_24h=resolved_alerts,
        chronic_in_new=chronic,
        kg_edges_today=edges_today,
        kg_edges_yesterday=previous.get("kg_edges"),
        kg_services_today=services_today,
        kg_services_yesterday=previous.get("kg_services"),
    )


def changes_section(report: ChangeReport) -> str:
    """A1. Δ-only since yesterday — что изменилось vs прошлого окна.

    Пример:
      📈 Changes since yesterday
      +12 new alerts (5 chronic) · -8 resolved · +47 KG edges

    Если `new_baseline=True` — секция показывает только сегодняшние counts
    с пометкой `(new baseline)`.
    """
    if report.new_baseline:
        return (
            "**📈 Changes since yesterday**\n"
            f"  `{report.new_alerts_24h}` new alerts · "
            f"`{report.resolved_alerts_24h}` resolved · "
            f"`{report.kg_edges_today}` KG edges _(new baseline)_"
        )

    parts: List[str] = []
    parts.append(
        f"`+{report.new_alerts_24h}` new alerts" + (
            f" ({report.chronic_in_new} chronic)" if report.chronic_in_new else ""
        )
    )
    parts.append(f"`-{report.resolved_alerts_24h}` resolved")

    if report.kg_edges_yesterday is not None:
        delta_e = report.kg_edges_today - report.kg_edges_yesterday
        sign = "+" if delta_e >= 0 else ""
        parts.append(f"`{sign}{delta_e}` KG edges")

    return "**📈 Changes since yesterday**\n  " + " · ".join(parts)


def _chronic_action_items(db: Session, threshold: int = 10) -> List[Dict[str, Any]]:
    """B4. Service+alertname пары с ≥threshold fires за 24h → RCA-кандидаты.

    Возвращает [{service, alertname, fires}, ...] sorted by fires desc, cap=3.
    """
    try:
        rows = db.execute(text("""
            SELECT s.name AS service, a.alertname, count(*) AS fires
            FROM kg_alerts a
            JOIN kg_services s ON s.id = a.service_id
            WHERE a.fired_at > NOW() - INTERVAL '24 hours'
              AND a.service_id IS NOT NULL
            GROUP BY s.name, a.alertname
            HAVING count(*) >= :threshold
            ORDER BY count(*) DESC
            LIMIT 3
        """), {"threshold": threshold}).fetchall()
        return [
            {"service": r[0], "alertname": r[1], "fires": int(r[2])}
            for r in rows
        ]
    except Exception as e:
        log.warning("stats_digest.chronic_action_items_failed", error=str(e))
        return []


def _unowned_action_items(db: Session) -> int:
    """B4. Сколько real services без owner после backfill.

    Возвращает count. Не возвращаем сами имена — это для footer-pill.
    """
    try:
        return int(db.execute(text("""
            SELECT count(*) FROM kg_services
            WHERE NOT synthetic
              AND (team_owner IS NULL OR team_owner = '')
        """)).scalar() or 0)
    except Exception:
        return 0


def _suspicious_stale_action_items(db: Session, days: int = 60) -> int:
    """B4. Services с stale_class=suspicious_stale без deploys >days.

    Возвращает count.
    """
    try:
        cnt = db.execute(text("""
            SELECT count(*) FROM kg_services s
            WHERE NOT synthetic
              AND s.stale_class = 'suspicious_stale'
              AND NOT EXISTS (
                  SELECT 1 FROM kg_deployments d
                  WHERE d.service_id = s.id
                    AND d.started_at > NOW() - (:days || ' days')::interval
              )
        """), {"days": str(days)}).scalar() or 0
        return int(cnt)
    except Exception as e:
        log.warning("stats_digest.suspicious_stale_failed", error=str(e))
        return 0


# ── DQ polish 2026-05-25: suspicious_stale drill-down helpers ────────────

def _suspicious_in_prod_with_alerts(
    db: Session, days: int = 60
) -> Tuple[int, List[str]]:
    """Suspicious-stale сервисы которые сидят в prod-* ns И при этом имеют
    хотя бы один firing alert (resolved_at IS NULL) — это самый острый
    actionable bucket: видимо deploys тоже не идут, а alerts горят.

    Возвращает (count, top_3_names).
    """
    try:
        rows = db.execute(text("""
            SELECT s.name
            FROM kg_services s
            WHERE NOT s.synthetic
              AND s.stale_class = 'suspicious_stale'
              AND s.namespace LIKE 'prod%'
              AND NOT EXISTS (
                  SELECT 1 FROM kg_deployments d
                  WHERE d.service_id = s.id
                    AND d.started_at > NOW() - (:days || ' days')::interval
              )
              AND EXISTS (
                  SELECT 1 FROM kg_alerts a
                  WHERE a.service_id = s.id
                    AND a.resolved_at IS NULL
              )
            ORDER BY s.name
        """), {"days": str(days)}).fetchall()
    except Exception as e:
        log.warning("stats_digest.suspicious_in_prod_failed", error=str(e))
        try:
            db.rollback()
        except Exception:
            pass
        return (0, [])
    names = [r[0] for r in rows if r and r[0]]
    return (len(names), names[:3])


def _suspicious_with_callers(db: Session, days: int = 60) -> int:
    """Suspicious-stale сервисы у которых ≥1 inbound caller (источник в
    kg_edges с target = svc). Полезные для других — нельзя просто удалить.
    """
    try:
        cnt = db.execute(text("""
            SELECT count(*) FROM kg_services s
            WHERE NOT s.synthetic
              AND s.stale_class = 'suspicious_stale'
              AND NOT EXISTS (
                  SELECT 1 FROM kg_deployments d
                  WHERE d.service_id = s.id
                    AND d.started_at > NOW() - (:days || ' days')::interval
              )
              AND EXISTS (
                  SELECT 1 FROM kg_edges e
                  WHERE e.target_id = s.id
              )
        """), {"days": str(days)}).scalar() or 0
        return int(cnt)
    except Exception as e:
        log.warning("stats_digest.suspicious_with_callers_failed", error=str(e))
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _suspicious_in_external_or_mcp(db: Session, days: int = 60) -> int:
    """Suspicious-stale сервисы где namespace или name содержит external/mcp.
    Скорее всего pet-проекты или экспериментальные, можно удалять смело.
    """
    try:
        cnt = db.execute(text("""
            SELECT count(*) FROM kg_services s
            WHERE NOT s.synthetic
              AND s.stale_class = 'suspicious_stale'
              AND NOT EXISTS (
                  SELECT 1 FROM kg_deployments d
                  WHERE d.service_id = s.id
                    AND d.started_at > NOW() - (:days || ' days')::interval
              )
              AND (
                  s.namespace ILIKE '%external%'
                  OR s.namespace ILIKE '%mcp%'
                  OR s.name ILIKE '%external%'
                  OR s.name ILIKE '%mcp%'
              )
        """), {"days": str(days)}).scalar() or 0
        return int(cnt)
    except Exception as e:
        log.warning("stats_digest.suspicious_external_mcp_failed", error=str(e))
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _suspicious_remaining(
    db: Session,
    total: int,
    prod_with_alerts: int,
    with_callers: int,
    external_or_mcp: int,
) -> int:
    """Остаток после вычитания всех buckets из total.

    NB: buckets могут пересекаться (prod сервис может иметь callers), но
    для action-items это нормально — мы не делаем strict partition, просто
    показываем «остальное в batch sweep». Аккуратно гарантируем >=0.
    """
    leftover = total - prod_with_alerts - with_callers - external_or_mcp
    if leftover < 0:
        return 0
    return leftover


def action_items_section(
    db: Session,
    *,
    chronic_threshold: int = 10,
    suspicious_days: int = 60,
) -> str:
    """B4. Action items — RCA-кандидаты вместо просто-наблюдательного списка.

    Три категории:
      1. Chronic (≥threshold fires/24h) → RCA нужен.
      2. Без owner → manual ownership.yaml.
      3. Suspicious stale ≥60d → review/delete.

    Если все три пусты — секция скрыта (return "").
    """
    chronic = _chronic_action_items(db, threshold=chronic_threshold)
    unowned = _unowned_action_items(db)
    stale = _suspicious_stale_action_items(db, days=suspicious_days)

    if not chronic and not unowned and not stale:
        return ""

    lines = ["**🎯 Action items**"]
    if chronic:
        services_str = ", ".join(
            f"{c['service']}" for c in chronic
        )
        lines.append(
            f"  • `{len(chronic)}` chronic alerts ≥{chronic_threshold} fires/24h "
            f"→ RCA: {services_str}"
        )
    if unowned:
        lines.append(
            f"  • `{unowned}` services без owner после backfill — нужен "
            f"manual в ownership.yaml"
        )
    if stale:
        # DQ polish 2026-05-25: drill-down — общий счётчик 2000+ не actionable,
        # разрезаем по priority buckets чтобы команда видела с чего начать.
        prod_cnt, prod_top = _suspicious_in_prod_with_alerts(
            db, days=suspicious_days
        )
        callers_cnt = _suspicious_with_callers(db, days=suspicious_days)
        ext_mcp_cnt = _suspicious_in_external_or_mcp(db, days=suspicious_days)
        remaining = _suspicious_remaining(
            db, stale, prod_cnt, callers_cnt, ext_mcp_cnt
        )

        lines.append(
            f"  • `{stale}` suspicious_stale без deploys >{suspicious_days}d "
            f"— нужен review:"
        )
        if prod_cnt:
            top_str = ""
            if prod_top:
                top_str = f" (e.g. {', '.join(f'`{n}`' for n in prod_top)})"
            lines.append(
                f"     - `{prod_cnt}` in prod/* (has firing alerts){top_str} → priority"
            )
        if callers_cnt:
            lines.append(
                f"     - `{callers_cnt}` with inbound_callers >0 → check if needed"
            )
        if ext_mcp_cnt:
            lines.append(
                f"     - `{ext_mcp_cnt}` in external/mcp (pet projects?) → likely delete"
            )
        if remaining:
            lines.append(
                f"     - остальные `{remaining}` → batch sweep"
            )
    return "\n".join(lines)


def noisemakers_section(fired_series: List[dict], threshold_pct: float = 20.0) -> str:
    """B5. Top-3 service генерирующих >threshold_pct% алертов.

    Группируем series по (namespace, service-label). Если ни один сервис не
    набрал ≥threshold% — секция скрыта.
    """
    if not fired_series:
        return ""

    total = len(fired_series)
    if total == 0:
        return ""

    counter: Counter = Counter()
    ns_map: Dict[str, str] = {}
    for s in fired_series:
        m = s.get("metric", {})
        # Резолв service-имени: пробуем явные labels, fallback на pod-prefix.
        svc = (
            m.get("service")
            or m.get("deployment")
            or m.get("statefulset")
        )
        if not svc:
            pod = m.get("pod")
            if pod:
                svc = pod.rsplit("-", 2)[0]
        if not svc or svc == "?":
            continue
        ns = m.get("namespace") or m.get("exported_namespace") or "?"
        counter[svc] += 1
        ns_map.setdefault(svc, ns)

    if not counter:
        return ""

    top = counter.most_common(3)
    notable = [(svc, cnt) for svc, cnt in top if (cnt / total * 100) >= threshold_pct]
    if not notable:
        return ""

    lines = ["**🔊 Noisemakers (24h)**"]
    for svc, cnt in notable:
        pct = (cnt / total * 100)
        ns = ns_map.get(svc, "")
        # DQ polish 2026-05-25: формат с `@<ns>` явно подчёркивает что это
        # namespace (старый italic-формат `_mcp_` смотрелся typo-подобно).
        # Если ns пустой/неизвестный — без `@` маркера.
        if ns and ns != "?":
            ns_part = f" @{ns}"
        else:
            ns_part = ""
        lines.append(
            f"  • `{svc}`{ns_part} — `{pct:.0f}%` alerts ({cnt} events)"
        )
    return "\n".join(lines)


def _mttr_stats(
    db: Session, days: int, offset_days: int = 0
) -> Optional[Dict[str, float]]:
    """Median + p95 MTTR (resolved_at - fired_at) for alerts resolved в окне.

    Окно: [now - (offset_days + days), now - offset_days). Non-overlapping
    windows для honest trend-сравнения (offset_days=days даёт prev period
    того же размера, не пересекающийся с current).

    DQ polish 2026-05-25: winsorize — durations >= 7d считаются "outliers"
    (backfill artefact, stuck alerts от старых backfill-ов до 22 мая) и не
    попадают в median/p95. Отдельный счётчик `outliers_gt_7d` рендерится
    в секции только если >0 — чтобы было видно что грязь есть, но цифры
    реалистичные.

    Возвращает None если 0 resolved в окне (после фильтрации).
    """
    try:
        row = db.execute(text("""
            SELECT
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (resolved_at - fired_at)) / 60
                ) FILTER (WHERE (resolved_at - fired_at) < INTERVAL '7 days')
                    AS median_min,
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (resolved_at - fired_at)) / 60
                ) FILTER (WHERE (resolved_at - fired_at) < INTERVAL '7 days')
                    AS p95_min,
                count(*) FILTER (WHERE (resolved_at - fired_at) < INTERVAL '7 days')
                    AS n,
                count(*) FILTER (WHERE (resolved_at - fired_at) >= INTERVAL '7 days')
                    AS outliers
            FROM kg_alerts
            WHERE resolved_at IS NOT NULL
              AND fired_at IS NOT NULL
              AND resolved_at >= fired_at
              AND resolved_at <  NOW() - (:offset_days * INTERVAL '1 day')
              AND resolved_at >= NOW() - ((:offset_days + :days) * INTERVAL '1 day')
        """), {"days": days, "offset_days": offset_days}).fetchone()
    except Exception as e:
        log.warning("stats_digest.mttr_query_failed", error=str(e))
        # Rollback aborted transaction чтобы последующие секции digest не падали
        # каскадом на InFailedSqlTransaction.
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if row is None:
        return None
    samples = int(row[2] or 0)
    outliers = int(row[3] or 0)
    if samples == 0:
        # Нет sane samples (<7d) — но если outliers были, всё равно вернём
        # минимальный dict чтобы вызывающая сторона могла отрисовать degraded
        # вариант. Текущий callsite mttr_section проверяет samples и скроет —
        # это окей; outliers видны через возвращаемый dict.
        if outliers == 0:
            return None
        return {
            "median_min": 0.0,
            "p95_min": 0.0,
            "samples": 0,
            "outliers_gt_7d": outliers,
        }
    return {
        "median_min": float(row[0] or 0),
        "p95_min": float(row[1] or 0),
        "samples": samples,
        "outliers_gt_7d": outliers,
    }


def mttr_section(db: Session, days: int = 7) -> str:
    """B6. MTTR mini-stat — median / p95 + trend vs prev week.

    `kg_alerts WHERE resolved_at >= now()-Nd`. Если нет samples — скрываем
    секцию. DQ polish 2026-05-25: значения winsorized (durations ≥7d
    исключены из median/p95). Отдельный counter outliers (>7d) рендерится
    только при наличии — чтобы видно было что грязь есть.
    """
    now_stats = _mttr_stats(db, days=days)
    if not now_stats or now_stats.get("samples", 0) == 0:
        return ""
    prev_stats = _mttr_stats(db, days=days * 2)  # rough: includes current; trend approx

    lines = [f"**⏱️ MTTR (resolved alerts last {days}d)**"]

    trend_str = ""
    if prev_stats and prev_stats.get("samples", 0) > now_stats["samples"]:
        # Очень грубая trend-эвристика: median 7d vs median 14d window.
        # Не идеальная, но даёт сигнал что-то меняется.
        prev_med = prev_stats["median_min"]
        cur_med = now_stats["median_min"]
        delta = cur_med - prev_med
        if abs(delta) >= 1:
            sign = "+" if delta > 0 else ""
            trend_str = f" · trend: `{sign}{delta:.0f}min` vs prev week"

    outliers = int(now_stats.get("outliers_gt_7d", 0) or 0)
    outliers_str = f" · outliers (>7d): `{outliers}`" if outliers > 0 else ""

    lines.append(
        f"  median: `{now_stats['median_min']:.0f}min` · "
        f"p95: `{now_stats['p95_min']:.0f}min` · "
        f"samples: `{now_stats['samples']}`{trend_str}{outliers_str}"
    )
    return "\n".join(lines)


def deploy_incident_correlation_section(db: Session, hours: int = 24) -> str:
    """B7. Deploy → incident correlation.

    JOIN kg_deployments × kg_alerts: service_id same AND alert fired_at
    в окне ≤30m после deploy.started_at. Возвращает success-rate + worst deploy.
    """
    try:
        # Сначала overall: сколько deploys, сколько attributed (≥1 alert в 30m).
        overall = db.execute(text("""
            WITH recent_deploys AS (
                SELECT id, service_id, started_at, status, build_number, triggered_by, extras
                FROM kg_deployments
                WHERE started_at > NOW() - (:hours || ' hours')::interval
            )
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM kg_alerts a
                    WHERE a.service_id = recent_deploys.service_id
                      AND a.fired_at BETWEEN recent_deploys.started_at
                                         AND recent_deploys.started_at + INTERVAL '30 minutes'
                )) AS attributed,
                count(*) FILTER (WHERE status = 'SUCCESS') AS successes
            FROM recent_deploys
        """), {"hours": str(hours)}).fetchone()
    except Exception as e:
        log.warning("stats_digest.deploy_incident_failed", error=str(e))
        return ""

    if overall is None:
        return ""
    total = int(overall[0] or 0)
    if total == 0:
        return ""
    attributed = int(overall[1] or 0)
    successes = int(overall[2] or 0)

    try:
        worst = db.execute(text("""
            SELECT
                d.build_number,
                d.triggered_by,
                count(a.id) AS alert_cnt
            FROM kg_deployments d
            JOIN kg_alerts a ON a.service_id = d.service_id
                AND a.fired_at BETWEEN d.started_at
                                   AND d.started_at + INTERVAL '30 minutes'
            WHERE d.started_at > NOW() - (:hours || ' hours')::interval
            GROUP BY d.id, d.build_number, d.triggered_by
            ORDER BY count(a.id) DESC
            LIMIT 1
        """), {"hours": str(hours)}).fetchone()
    except Exception:
        worst = None

    success_pct = (successes / total * 100) if total else 0.0
    attributed_pct = (attributed / total * 100) if total else 0.0

    lines = [f"**🚀 Deploy → incident correlation ({hours}h)**"]
    lines.append(
        f"  `{total}` deploys · `{attributed}` attributed alerts "
        f"({attributed_pct:.0f}%) · success rate `{success_pct:.0f}%`"
    )

    # DQ polish 2026-05-25: если attributed=0 при большом N — это почти
    # наверняка не "deploy-free день", а linkage gap (service_id NULL на
    # одной из сторон JOIN-а). Покажем диагностику чтобы было понятно
    # где править.
    if attributed == 0 and total >= 10:
        deploys_linked_pct, alerts_linked_pct = _deploy_correlation_diagnostics(
            db, hours=hours
        )
        lines.append(
            f"  ⚠️ Likely linkage gap: `{deploys_linked_pct:.0f}%` deploys linked "
            f"to service, `{alerts_linked_pct:.0f}%` alerts linked"
        )

    if worst and int(worst[2] or 0) >= 2:
        b_num, b_user, b_cnt = worst
        user_str = f"by `{b_user}`" if b_user else "_auto_"
        lines.append(
            f"  Worst: Build #{b_num} {user_str} → `{int(b_cnt)}` alerts in 30m "
            f"(rollback recommended)"
        )
    return "\n".join(lines)


def _deploy_correlation_diagnostics(
    db: Session, hours: int = 24
) -> Tuple[float, float]:
    """DQ helper: % rows с not-NULL service_id в kg_deployments / kg_alerts
    в окне hours.

    Используется чтобы при attributed=0 показать, что причина — отсутствие
    привязки к service_id, а не реальное «всё чисто после деплоя».
    Возвращает (deploys_linked_pct, alerts_linked_pct). При ошибках — (0,0).
    """
    try:
        row = db.execute(text("""
            WITH d AS (
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE service_id IS NOT NULL) AS linked
                FROM kg_deployments
                WHERE started_at > NOW() - (:hours || ' hours')::interval
            ),
            a AS (
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE service_id IS NOT NULL) AS linked
                FROM kg_alerts
                WHERE fired_at > NOW() - (:hours || ' hours')::interval
            )
            SELECT d.total, d.linked, a.total, a.linked FROM d, a
        """), {"hours": str(hours)}).fetchone()
    except Exception as e:
        log.warning("stats_digest.deploy_diagnostics_failed", error=str(e))
        try:
            db.rollback()
        except Exception:
            pass
        return (0.0, 0.0)
    if row is None:
        return (0.0, 0.0)
    d_total = int(row[0] or 0)
    d_linked = int(row[1] or 0)
    a_total = int(row[2] or 0)
    a_linked = int(row[3] or 0)
    d_pct = (d_linked / d_total * 100) if d_total else 0.0
    a_pct = (a_linked / a_total * 100) if a_total else 0.0
    return (d_pct, a_pct)


async def topology_growth_section(db: Session) -> str:
    """B8. Δ services / edges / new NATS subjects vs Redis snapshot.

    Если snapshot ещё не записан (first run — fresh deployment, Redis-flush,
    TTL истёк) — рисуем pill `(new baseline · counting starts now)` вместо
    abs-count diff. Иначе показывали бы `+2909 services since yesterday`,
    хотя реально за окно создалось ~200 (реальный сценарий 25 мая).
    Snapshot сохраняем в любом случае — следующий run даст нормальный Δ.
    """
    services_now = _count_real_services(db)
    edges_now = _count_edges(db)
    subjects_now = _nats_subjects(db)

    prev = await _read_topology_snapshot()
    # Запишем сегодняшний snapshot в любом случае (включая first-run).
    await _write_topology_snapshot({
        "services": services_now,
        "edges": edges_now,
        "nats_subjects": subjects_now,
    })

    if _detect_first_run(prev):
        # First-run pill — не показываем Δ, чтобы не пугать «+N services since
        # yesterday», где N = весь существующий граф. Завтра будет нормальный
        # diff. См. live digest 25 мая 2026 / regression note.
        return (
            "**🧬 Topology growth (24h)**\n"
            f"  `{services_now}` services · `{edges_now}` edges "
            "_(new baseline · counting starts now)_"
        )

    assert prev is not None  # narrow для mypy после _detect_first_run
    prev_services = int(prev.get("services") or 0)
    prev_edges = int(prev.get("edges") or 0)
    prev_subjects = set(prev.get("nats_subjects") or [])

    d_services = services_now - prev_services
    d_edges = edges_now - prev_edges
    new_subjects = sorted(set(subjects_now) - prev_subjects)

    if d_services == 0 and d_edges == 0 and not new_subjects:
        return ""

    parts: List[str] = []
    if d_services:
        sign = "+" if d_services > 0 else ""
        parts.append(f"`{sign}{d_services}` services")
    if d_edges:
        sign = "+" if d_edges > 0 else ""
        parts.append(f"`{sign}{d_edges}` edges")
    if new_subjects:
        shown = ", ".join(new_subjects[:3])
        more = f" +{len(new_subjects) - 3} more" if len(new_subjects) > 3 else ""
        parts.append(f"`+{len(new_subjects)}` NATS subjects ({shown}{more})")

    return "**🧬 Topology growth (24h)**\n  " + " · ".join(parts)


def pipeline_health_section(db: Session, stale_minutes: Optional[int] = None) -> str:
    """C9. Pipeline gauge header — vmsingle/vmagent/AM/copilot/seq freshness.

    Two-tier check:
      1. **task.last_run** (Redis heartbeat, см. `_get_beat_last_run`) —
         задача *запускается*? Сравниваем с expected_interval*2. Это
         основной gauge: если beat scheduler жив и task не падает, мы видим
         `✓` даже если *данные* отстают (VM scrape gap, downstream API down).
      2. **data lag** (fallback на `check_sync_lag` для отсутствующего
         heartbeat'а + data-stale warning) — если task ходит, но materialized
         timestamp отстаёт > stale_minutes, рисуем отдельный `data lag Xh`
         мессадж. Это разделяет «scheduled OK, data lag» от «task завис».

    Regression note (live digest 25 мая 2026): старая версия рисовала
    `vmsingle/vmalert/AM ⚠️ 2h gap` хотя kg_metrics_sync ходил каждые 10 мин;
    причиной было чтение data-timestamp (`max(ServiceHealth.ts)`), а не
    timestamp последнего запуска task'а. См. также `ref_wo_vm_scrape_gap`.

    stale_minutes default — settings.STATS_PIPELINE_STALE_MINUTES (60).
    """
    if stale_minutes is None:
        stale_minutes = getattr(settings, "STATS_PIPELINE_STALE_MINUTES", 60)
    try:
        from app.knowledge_graph.self_health import check_sync_lag
        result = check_sync_lag(db)
        per_task = result.detail.get("per_task", {})
    except Exception as e:
        log.warning("stats_digest.pipeline_health_failed", error=str(e))
        return ""

    # Map sync_task → display name.
    display_map = {
        "kg_metrics_sync": "vmsingle",
        "kg_cluster_health_sync": "vmagent",
        "kg_anomaly_detection_task": "vmalert",
        "kg_topology_sync": "copilot",
        "kg_seq_logs_sync": "seq",
        "kg_signal_aggregates_compute": "AM",
    }
    now = datetime.now(timezone.utc)
    parts: List[str] = []
    data_lag_parts: List[str] = []
    for task_name, display in display_map.items():
        info = per_task.get(task_name)
        expected_interval = _BEAT_TASK_INTERVAL_MINUTES.get(task_name)
        last_run = _get_beat_last_run(task_name) if expected_interval else None

        # Tier 1: prefer task.last_run, если heartbeat есть.
        if last_run is not None and expected_interval is not None:
            # Защита от naive datetime — приводим к timezone-aware UTC.
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            task_lag_min = (now - last_run).total_seconds() / 60.0
            task_healthy = task_lag_min <= expected_interval * 2
            if task_healthy:
                parts.append(f"{display} ✓")
            else:
                gap = _format_gap_minutes(task_lag_min)
                parts.append(f"{display} ⚠️ {gap} since last run")

            # Дополнительный сигнал: task healthy, но данные stale.
            # Например VM scrape gap → kg_metrics_sync ходит, но 0 rows.
            data_lag_min = info.get("lag_minutes") if info else None
            if (
                task_healthy
                and data_lag_min is not None
                and data_lag_min > stale_minutes
            ):
                gap = _format_gap_minutes(data_lag_min)
                data_lag_parts.append(f"{display}: scheduled OK, data lag {gap}")
            continue

        # Tier 2 fallback: heartbeat недоступен → старая логика по data ts.
        if info is None:
            continue
        lag_min = info.get("lag_minutes")
        if lag_min is None:
            parts.append(f"{display} ⚠️ no data")
            continue
        if lag_min > stale_minutes:
            gap = _format_gap_minutes(lag_min)
            parts.append(f"{display} ⚠️ {gap} gap")
        else:
            parts.append(f"{display} ✓")

    if not parts:
        return ""
    line = "**📡 Pipeline**\n  " + " · ".join(parts)
    if data_lag_parts:
        line += "\n  _" + " · ".join(data_lag_parts) + "_"
    return line


def _format_gap_minutes(lag_min: float) -> str:
    """`5m` / `90m` → `1h` / `2h` (часы при >= 60m). Используется gauge'ами."""
    hours = lag_min / 60
    if hours >= 1:
        return f"{hours:.0f}h"
    return f"{lag_min:.0f}m"


def beat_heartbeats_footer(db: Session) -> str:
    """C10. Beat-task heartbeats — last_run per sync task. Footer-row.

    Использует тот же `check_sync_lag` source. Формат:
      Syncs: metrics 14:45 · cluster 14:30 · topology 12:17 (5h ago) · 4/4 active
    """
    try:
        from app.knowledge_graph.self_health import check_sync_lag
        result = check_sync_lag(db)
        per_task = result.detail.get("per_task", {})
    except Exception as e:
        log.warning("stats_digest.beat_heartbeats_failed", error=str(e))
        return ""

    short_names = {
        "kg_metrics_sync": "metrics",
        "kg_cluster_health_sync": "cluster",
        "kg_topology_sync": "topology",
        "kg_seq_logs_sync": "seq",
        "kg_anomaly_detection_task": "anomaly",
        "kg_signal_aggregates_compute": "aggregates",
    }
    parts: List[str] = []
    active = 0
    total = 0
    for task_name, short in short_names.items():
        info = per_task.get(task_name)
        if info is None:
            continue
        total += 1
        lag_min = info.get("lag_minutes")
        last_ts = info.get("last_ts")
        if lag_min is None or last_ts is None:
            continue
        active += 1
        try:
            dt = datetime.fromisoformat(last_ts)
            hhmm = dt.strftime("%H:%M")
        except Exception:
            hhmm = "?"
        ago = ""
        if lag_min > 60:
            ago = f" ({lag_min/60:.0f}h ago)"
        parts.append(f"{short} {hhmm}{ago}")

    if not parts:
        return ""
    return f"_Syncs: {' · '.join(parts)} · {active}/{total} active_"


# ── build_digest entry ─────────────────────────────────────────────────────


async def build_digest(db: Session) -> str:
    """Собрать полный digest. Возвращает markdown-string для Discord.

    Совместимость с pre-overhaul callers (тестами): тонкая обёртка над
    `_build_digest_with_meta`, которая возвращает только content без meta.
    """
    content, _ = await _build_digest_with_meta(db)
    return content


async def _build_digest_with_meta(db: Session) -> Tuple[str, Dict[str, Any]]:
    """Собрать полный digest + meta. Возвращает (markdown, meta).

    `meta` — диагностика для skip-if-noop:
      - sections_with_content: int — сколько не-пустых секций (помимо
        cluster_health и pipeline_health которые рисуются всегда).
      - change_report: ChangeReport (для тестов).
    """
    ns_to_team = _get_ns_to_team_map(db)
    fired_series: List[dict] = []

    if settings.VICTORIA_METRICS_URL:
        vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=15.0)
        try:
            fired_series = await vm.query_range(
                'ALERTS{alertstate="firing"}',
                datetime.now(timezone.utc) - timedelta(minutes=5),
                datetime.now(timezone.utc),
                step="30s",
            )
        except Exception as e:
            log.warning("stats_digest.vm_query_failed", error=str(e))

    # Item #3: trend для Firing series vs последнего daily snapshot.
    firing_yesterday = await _read_last_firing_series()
    # Item A1: full prev-day snapshot для Δ-секции.
    prev_snapshot = await _read_day_snapshot()

    if settings.VICTORIA_METRICS_URL:
        health_text = await cluster_health_section(
            VMClient(settings.VICTORIA_METRICS_URL, timeout=15.0),
            fired_series,
            db,
            firing_series_yesterday=firing_yesterday,
        )
    else:
        health_text = "**🛡️ Cluster Health**\n  _VICTORIA_METRICS_URL не настроен_"

    # Item A1: ChangeReport — diff vs prev snapshot.
    # crashloops_today берётся из VM cluster_health; для простоты передаём None,
    # детальные deltas достаём из current_crashloops через VM-snapshot уже в
    # cluster_health_section, тут считаем только alerts/edges.
    change_report = await _compute_change_report(
        db,
        firing_today=len(fired_series),
        crashloops_today=None,
        previous=prev_snapshot,
    )

    alerts_text, unique_alerts, _, unowned_ns = firing_alerts_section(
        fired_series, ns_to_team
    )
    unowned_text = unowned_namespaces_section(unowned_ns, db)
    top_types_text = top_alert_types_section(unique_alerts, db)
    anomaly_summary_text = anomaly_summary_section(db)
    anomaly_top_text = anomaly_top_section(db, ns_to_team)
    log_errors_text = log_errors_section(db, ns_to_team)
    fragile_text = fragile_services_section(db, ns_to_team)
    stale_text = stale_deployments_section(
        db, ns_to_team, threshold_days=settings.STATS_DIGEST_STALE_DAYS
    )
    deploys_text = await recent_deploys_section(lookback_hours=24, limit=5)
    kg_text = kg_quality_section(db)

    # Overhaul sections.
    pipeline_text = pipeline_health_section(db)
    changes_text = changes_section(change_report)
    actions_text = action_items_section(db)
    noise_text = noisemakers_section(fired_series)
    mttr_text = mttr_section(db, days=7)
    deploy_corr_text = deploy_incident_correlation_section(db, hours=24)
    topology_text = await topology_growth_section(db)
    heartbeats_text = beat_heartbeats_footer(db)

    # Item #3: записать сегодняшний firing count для завтрашнего trend.
    # Делаем ПОСЛЕ всех queries — чтобы не аффектить today-сравнение если бы
    # кто-то перечитал ключ.
    await _write_last_firing_series(len(fired_series))
    # Item A1: записать full snapshot для завтрашнего Δ.
    await _write_day_snapshot({
        "firing_series": len(fired_series),
        "crashloops": None,  # cluster_health сам пишет свой ключ
        "kg_edges": change_report.kg_edges_today,
        "kg_services": change_report.kg_services_today,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Item A2: skip-if-noop — meta-счётчик не-пустых action/Δ секций.
    # Cluster health, pipeline и kg_quality рисуются всегда, не считаем.
    actionable_sections = [
        changes_text, actions_text, noise_text, mttr_text, deploy_corr_text,
        topology_text, alerts_text if "ни одной серии" not in alerts_text else "",
        unowned_text, top_types_text if "нет активных алертов" not in top_types_text else "",
        anomaly_summary_text if "ни одной аномалии" not in anomaly_summary_text else "",
        anomaly_top_text, log_errors_text, fragile_text if "нет edges" not in fragile_text else "",
        stale_text if "ничего не stale" not in stale_text and "ничего suspicious" not in stale_text else "",
        deploys_text,
    ]
    sections_with_content = sum(1 for s in actionable_sections if s)

    # Пустые секции (вернувшие "") пропускаем — иначе \n\n.join создаст
    # двойной перевод строки и в Discord будет пустая дыра.
    sections = [
        f"📊 **Cluster Daily Digest** · {now}",
        pipeline_text,
        health_text,
        changes_text,
        actions_text,
        alerts_text,
        unowned_text,
        top_types_text,
        noise_text,
        mttr_text,
        deploy_corr_text,
        topology_text,
        anomaly_summary_text,
        anomaly_top_text,
        log_errors_text,
        deploys_text,
        fragile_text,
        stale_text,
        kg_text,
        heartbeats_text,
    ]
    content = "\n\n".join(s for s in sections if s)

    return content, {
        "sections_with_content": sections_with_content,
        "change_report": change_report,
        "fired_series_count": len(fired_series),
    }


async def send_daily_digest(db: Session) -> Dict[str, Any]:
    """Точка входа из Celery beat task — собрать и отправить.

    Item A2 — skip-if-noop: если все actionable секции пусты И нет changes
    относительно прошлого окна, не постим вообще. Settings гvr
    `STATS_DIGEST_SKIP_NOOP` (default True) контролирует поведение.
    """
    if not settings.STATS_DIGEST_ENABLED:
        log.info("stats_digest.skipped", reason="STATS_DIGEST_ENABLED=false")
        return {"status": "skipped", "reason": "disabled"}

    # _build_digest_with_meta — основной path. build_digest остаётся как
    # backwards-compat обёртка для тестов которые мокают её напрямую (см.
    # test_send_daily_digest_sends_when_enabled). Внутри Celery beat task
    # этот path не используется — там build_digest не мокается.
    content, meta = await _build_digest_with_meta(db)
    cr: ChangeReport = meta["change_report"]

    skip_noop = getattr(settings, "STATS_DIGEST_SKIP_NOOP", True)
    if skip_noop and meta["sections_with_content"] == 0:
        # Все секции пусты И нет deltas → silently skip.
        # New-baseline run считаем noop если 0 alerts/0 edges-delta (всё равно
        # завтра будет нормальный digest).
        if (
            cr.new_alerts_24h == 0
            and cr.resolved_alerts_24h == 0
            and meta["fired_series_count"] == 0
        ):
            log.info(
                "stats_digest.skipped_noop",
                window="24h",
                content_len=len(content),
            )
            return {"status": "skipped_noop", "reason": "all sections empty"}

    # Импорт locally чтобы избежать circular-import на старте модуля.
    from app.services.discord_service import discord_service
    await discord_service.send_stats_report(content)
    return {"status": "sent", "content_len": len(content)}
