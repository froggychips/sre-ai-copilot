"""Daily stats digest для Discord #stats канала.

ПОЛНОСТЬЮ data-aggregation. Не делает inference. Инвариант проверяется
тестом `tests/test_stats_digest_no_llm.py` — он грепает source на запрещённые
символы и завалится при попытке импорта reasoning-агентов.

Секции (в порядке вывода):
  1. cluster_health — VMClient.get_cluster_health() + count firing series
  2. firing_alerts_by_squad — группировка firing-series по namespace→team_owner из KG
  3. top_alert_types — top-5 по alertname
  4. fragile_services — top-5 services с самым высоким inbound-degree в KG
  5. stale_deployments — kubectl-обход WO-namespaces, idle ≥ STATS_DIGEST_STALE_DAYS
  6. kg_quality — services/edges/orphan%/team_owner coverage

Запускается через Celery beat task `daily_stats_digest`
(см. app/workers/tasks.py).
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient

log = structlog.get_logger()


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


async def cluster_health_section(vm: VMClient, fired_series: List[dict]) -> str:
    """1. Cluster health snapshot."""
    try:
        ch = await vm.get_cluster_health()
        d = ch.to_dict() if hasattr(ch, "to_dict") else {}
        nodes = d.get("nodes_ready", "?")
        crash = d.get("crashloops", "?")
    except Exception as e:
        log.warning("stats_digest.cluster_health_failed", error=str(e))
        nodes, crash = "?", "?"
    return (
        "**🛡️ Cluster Health**\n"
        f"  Nodes ready: `{nodes}` · Crashloops: `{crash}` · Firing series: `{len(fired_series)}`"
    )


def firing_alerts_section(
    fired_series: List[dict],
    ns_to_team: Dict[str, str],
) -> Tuple[str, Counter, defaultdict]:
    """2. Firing-series, grouped by squad (через KG namespace→team).

    Возвращает (rendered_text, unique_alertnames, team_alerts) — второе и
    третье для использования в top_alert_types_section и для DI.
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
        # Inline-формат «@team Nс, ...» — раньше каждая team была отдельной
        # строкой (6-7 строк), теперь одна.
        sorted_teams = sorted(team_alerts, key=lambda t: -team_alerts[t])
        if sorted_teams:
            parts = ", ".join(f"`@{t}` {team_alerts[t]}с" for t in sorted_teams)
            lines.append(f"  {parts}")
        if unowned:
            top = sorted(unowned.items(), key=lambda x: -x[1])[:3]
            parts = ", ".join(f"{n}={c}" for n, c in top)
            lines.append(f"  _unowned_: {parts}")
    return "\n".join(lines), unique_alerts, team_alerts


# Noise-алерты Prometheus stack-а — не реальные проблемы, фильтруем
# из top-N чтобы не зашумлять digest. InfoInhibitor/Watchdog — служебные
# meta-alerts; CPUThrottlingHigh без severity критерия — часто false positive
# на bursty workload.
_NOISE_ALERTNAMES = frozenset({"InfoInhibitor", "Watchdog", "CPUThrottlingHigh"})


def top_alert_types_section(unique_alerts: Counter) -> str:
    """3. Top-3 alertname по числу series, без infrastructure-noise."""
    filtered = Counter({
        name: cnt for name, cnt in unique_alerts.items()
        if name not in _NOISE_ALERTNAMES
    })
    lines = ["**📋 Top alert types** (без infra-noise)"]
    if not filtered:
        lines.append("  _нет активных алертов_")
    else:
        for name, cnt in filtered.most_common(3):
            lines.append(f"  `{name}` × {cnt}")
    return "\n".join(lines)


def fragile_services_section(db: Session, ns_to_team: Dict[str, str]) -> str:
    """4. Top inbound-degree services (кто страдает при падении больше всех)."""
    rows = db.execute(text("""
        SELECT s.name, s.namespace, count(e.id) AS callers
        FROM kg_services s
        JOIN kg_service_edges e ON e.dst_id = s.id
        WHERE s.team_owner IS NULL OR s.team_owner != 'platform'
        GROUP BY s.id
        ORDER BY callers DESC
        LIMIT 3
    """)).fetchall()
    lines = ["**🔗 Top fragile services** (inbound callers из KG)"]
    if not rows:
        lines.append("  _нет edges_")
    else:
        for name, ns, callers in rows:
            team = ns_to_team.get(ns, "(unowned)")
            lines.append(f"  `{name}` _{ns}_ — {callers} callers · @{team}")
    return "\n".join(lines)


def stale_deployments_section(
    db: Session,
    ns_to_team: Dict[str, str],
    threshold_days: int,
    *,
    kubectl_fn=None,
) -> str:
    """5. Deployments живые (replicas>0) но spec не апдейтился ≥ threshold_days.

    Compact-rendering: deployments с одинаковым name и одинаковым idle_days
    встречающиеся в 3+ namespace-ах рендерятся одной строкой
    (`town-db-backup × 5 kingdoms · 62d`).

    Per-release deployer name через TC недостижим (TC устроен «buildtype per
    pipeline action», не «buildtype per helm-release»), поэтому not shown.
    Кто что катил отображается в `recent_deploys_section` отдельно.

    `kubectl_fn` — для DI в тестах.
    """
    fn = kubectl_fn or _kubectl_get_deployments_json

    wo_namespaces = sorted({
        ns for (ns,) in db.execute(text("SELECT DISTINCT namespace FROM kg_services")).fetchall()
    })

    now = datetime.now(timezone.utc)
    # entries: (idle, ns, name, team, replicas, last_update_dt)
    entries: List[Tuple[int, str, str, str, int, datetime]] = []
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
            team = ns_to_team.get(ns, "(unowned)")
            entries.append((idle, ns, name, team, replicas, last))

    lines = [f"**⏳ Stale deployments** (alive, не катились ≥{threshold_days}d)"]
    if not entries:
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

    `fetch_fn` — для DI в тестах. По умолчанию `teamcity_service.recent_deploys`.
    """
    if fetch_fn is None:
        from app.services.teamcity_service import recent_deploys as _rd
        fetch_fn = _rd
    try:
        builds = await fetch_fn(lookback_hours=lookback_hours, limit=limit)
    except Exception as e:
        log.warning("stats_digest.recent_deploys_failed", error=str(e))
        return ""

    # Если TC не сконфигурирован или за окно нет deploy-билдов — секцию вообще
    # не показываем, чтобы не шуметь в digest. Header без данных бесполезен.
    if not builds:
        return ""
    lines = [f"**🔧 Recent deploys** (последние {lookback_hours}h)"]

    now = datetime.now(timezone.utc)
    for b in builds:
        user = b.get("triggered_by")
        trig_type = b.get("triggered_type") or "?"
        actor = f"`{user}`" if user else f"_{trig_type}_"
        btype = b.get("buildtype_name") or "?"
        branch = (b.get("branch") or "?").replace("refs/heads/", "")
        num = b.get("number") or "?"
        status = b.get("status") or "?"
        ago = _humanize_ago(b.get("finished_at"), now)
        status_marker = "" if status == "SUCCESS" else f" · ⚠️ {status}"
        lines.append(
            f"  • by {actor} · `{btype}` ({branch} #{num}) · {ago}{status_marker}"
        )
    return "\n".join(lines)


def kg_quality_section(db: Session) -> str:
    """6. KG quality: services, orphan%, edges, team_owner coverage."""
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


async def build_digest(db: Session) -> str:
    """Собрать полный digest. Возвращает markdown-string для Discord."""
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

    if settings.VICTORIA_METRICS_URL:
        health_text = await cluster_health_section(
            VMClient(settings.VICTORIA_METRICS_URL, timeout=15.0),
            fired_series,
        )
    else:
        health_text = "**🛡️ Cluster Health**\n  _VICTORIA_METRICS_URL не настроен_"

    alerts_text, unique_alerts, _ = firing_alerts_section(fired_series, ns_to_team)
    top_types_text = top_alert_types_section(unique_alerts)
    fragile_text = fragile_services_section(db, ns_to_team)
    stale_text = stale_deployments_section(
        db, ns_to_team, threshold_days=settings.STATS_DIGEST_STALE_DAYS
    )
    deploys_text = await recent_deploys_section(lookback_hours=24, limit=5)
    kg_text = kg_quality_section(db)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Пустые секции (вернувшие "") пропускаем — иначе \n\n.join создаст
    # двойной перевод строки и в Discord будет пустая дыра.
    sections = [
        f"📊 **Cluster Daily Digest** · {now}",
        health_text,
        alerts_text,
        top_types_text,
        deploys_text,
        fragile_text,
        stale_text,
        kg_text,
    ]
    return "\n\n".join(s for s in sections if s)


async def send_daily_digest(db: Session) -> Dict[str, Any]:
    """Точка входа из Celery beat task — собрать и отправить."""
    if not settings.STATS_DIGEST_ENABLED:
        log.info("stats_digest.skipped", reason="STATS_DIGEST_ENABLED=false")
        return {"status": "skipped", "reason": "disabled"}

    content = await build_digest(db)

    # Импорт locally чтобы избежать circular-import на старте модуля.
    from app.services.discord_service import discord_service
    await discord_service.send_stats_report(content)
    return {"status": "sent", "content_len": len(content)}
