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
        for team in sorted(team_alerts, key=lambda t: -team_alerts[t]):
            lines.append(f"  `@{team}` — {team_alerts[team]} series")
        if unowned:
            top = sorted(unowned.items(), key=lambda x: -x[1])[:5]
            parts = ", ".join(f"{n}={c}" for n, c in top)
            lines.append(f"  _unowned ns_: {parts}")
    return "\n".join(lines), unique_alerts, team_alerts


def top_alert_types_section(unique_alerts: Counter) -> str:
    """3. Top-5 alertname по числу series."""
    lines = ["**📋 Top alert types**"]
    if not unique_alerts:
        lines.append("  _нет активных алертов_")
    else:
        for name, cnt in unique_alerts.most_common(5):
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
        LIMIT 5
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

    Игнорирует системные namespace-ы (kube-*, monitoring и т.п. — берём
    только те, что есть в KG). `kubectl_fn` — для DI в тестах.
    """
    fn = kubectl_fn or _kubectl_get_deployments_json

    wo_namespaces = sorted({
        ns for (ns,) in db.execute(text("SELECT DISTINCT namespace FROM kg_services")).fetchall()
    })

    now = datetime.now(timezone.utc)
    stale: List[Tuple[int, str, str, str, int, str]] = []
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
            helm = (dep["metadata"].get("annotations") or {}).get(
                "meta.helm.sh/release-name", "?"
            )
            stale.append((idle, ns, name, team, replicas, helm))

    stale.sort(key=lambda x: -x[0])
    lines = [f"**⏳ Stale deployments** (alive, не катились ≥{threshold_days}d)"]
    if not stale:
        lines.append("  ✅ ничего не stale")
        return "\n".join(lines)

    by_team: defaultdict = defaultdict(list)
    for entry in stale[:15]:  # cap общий список
        by_team[entry[3]].append(entry)
    for team, items in sorted(by_team.items()):
        lines.append(f"  `@{team}` — {len(items)} stale:")
        for idle, ns, name, _t, replicas, helm in items[:5]:
            lines.append(
                f"    • `{name}` _{ns}_ — idle **{idle}d** · {replicas} rs · helm=`{helm}`"
            )
    if len(stale) > 15:
        lines.append(f"  _… и ещё {len(stale) - 15} (скрыто)_")
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
    orphan = db.execute(text("""
        SELECT count(*) FROM kg_services s
        WHERE s.id NOT IN (
            SELECT src_id FROM kg_service_edges
            UNION SELECT dst_id FROM kg_service_edges
        )
    """)).scalar() or 0
    teamed = db.execute(
        text("SELECT count(*) FROM kg_services WHERE team_owner IS NOT NULL")
    ).scalar() or 0
    by_team = db.execute(text("""
        SELECT COALESCE(team_owner, '(unowned)') AS t, count(*)
        FROM kg_services GROUP BY team_owner ORDER BY count DESC LIMIT 6
    """)).fetchall()

    pct_orphan = (100 * orphan // services_total) if services_total else 0
    pct_team = (100 * teamed // services_total) if services_total else 0
    edges_str = ", ".join(f"{k}={v}" for k, v in sorted(edges_by_kind.items()))
    teams_str = ", ".join(f"@{t}={c}" for t, c in by_team)

    return "\n".join([
        "**🧬 KG quality**",
        f"  Services: `{services_total}` · Orphan: `{orphan}` ({pct_orphan}%)",
        f"  Edges: `{edges_total}` ({edges_str})",
        f"  Team-owned: `{teamed}` ({pct_team}%) → {teams_str}",
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
    kg_text = kg_quality_section(db)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n\n".join([
        f"📊 **Cluster Daily Digest** · {now}",
        health_text,
        alerts_text,
        top_types_text,
        fragile_text,
        stale_text,
        kg_text,
    ])


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
