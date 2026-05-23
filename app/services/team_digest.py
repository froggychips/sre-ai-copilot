"""Per-team daily digest для Discord.

Один embed per team_owner (squad-N / monitoring / infra / …) с агрегатами
за последние window_hours часов. Pure data-aggregation, БЕЗ LLM.

Источники данных (в порядке убывания «свежести»):
  * kg_signal_aggregates  — pre-computed per-service агрегаты (deploy_count,
                            failure_pct, alert_open_count, slo_burn_pct).
                            Hourly task `kg_signal_aggregates_compute`.
                            Если пусто (новая БД) — fallback на live-запросы.
  * kg_services.health_score — top-5 fragile.
  * kg_alerts             — open + severity-breakdown + top alertname.
  * kg_deployments        — deploy success-rate за окно.

Stylistic note: формат секций — как в `app/services/stats_digest.py`,
embed-структура — как `send_stats_report` (title + description + fields).

Запускается через Celery beat task `team_daily_digest_task`
(см. app/workers/tasks.py).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.knowledge_graph.schema import (AlertEvent, Deployment, Service,
                                        SignalAggregate)
from app.knowledge_graph.stuck_alerts import (find_stuck_alerts,
                                              severity_emoji)

log = structlog.get_logger("team_digest")

# Discord embed colour codes — те же что в discord_service.py,
# но локально, чтобы не плодить cross-module import только ради констант.
_COLOR_CRITICAL = 0xE53935   # red — есть open critical alerts / SLO burn высокий
_COLOR_WARNING  = 0xFDD835   # yellow — fragile есть, но всё под контролем
_COLOR_OK       = 0x43A047   # green — здоровый team
_COLOR_NEUTRAL  = 0x607D8B   # blue-grey — данных нет / новый team

# Максимальная глубина списков в embed-полях.
_TOP_FRAGILE = 5
_TOP_ALERTS = 5
_TOP_STUCK = 5


# ────────────────────────────────────────────────────────────────────────────
# Aggregation
# ────────────────────────────────────────────────────────────────────────────


def _top_fragile_services(
    db: Session, team_owner: str, limit: int = _TOP_FRAGILE,
) -> List[Dict[str, Any]]:
    """Top-N сервисов с наименьшим health_score (asc). NULL пропускаем."""
    rows = (
        db.query(Service.name, Service.namespace, Service.health_score)
        .filter(
            Service.team_owner == team_owner,
            Service.synthetic.is_(False),
            Service.health_score.isnot(None),
        )
        .order_by(Service.health_score.asc())
        .limit(limit)
        .all()
    )
    return [
        {"name": r.name, "namespace": r.namespace, "health_score": float(r.health_score)}
        for r in rows
    ]


def _deploy_stats(
    db: Session, team_owner: str, since_naive: datetime,
) -> Dict[str, Any]:
    """Deploy success-rate за окно. Берём всё что finished_at IS NOT NULL
    (RUNNING не считаем — статус пока не финальный)."""
    rows = (
        db.query(Deployment.status, func.count(Deployment.id))
        .join(Service, Service.id == Deployment.service_id)
        .filter(
            Service.team_owner == team_owner,
            Deployment.started_at >= since_naive,
            Deployment.finished_at.isnot(None),
        )
        .group_by(Deployment.status)
        .all()
    )
    total = 0
    success = 0
    failed = 0
    for status, cnt in rows:
        total += cnt
        st = (status or "").upper()
        if st == "SUCCESS":
            success += cnt
        elif st in ("FAILURE", "FAILED", "ERROR"):
            failed += cnt
    success_pct = (success / total * 100.0) if total else None
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "success_pct": success_pct,
    }


def _alerts_breakdown(
    db: Session, team_owner: str, since_naive: datetime,
) -> Dict[str, Any]:
    """Open alerts (resolved_at IS NULL) + severity counters + top alertname."""
    rows = (
        db.query(AlertEvent.severity, AlertEvent.alertname)
        .join(Service, Service.id == AlertEvent.service_id)
        .filter(
            Service.team_owner == team_owner,
            AlertEvent.fired_at >= since_naive,
            AlertEvent.resolved_at.is_(None),
        )
        .all()
    )
    severity_counter: Counter = Counter()
    alertname_counter: Counter = Counter()
    for sev, name in rows:
        severity_counter[(sev or "unknown").lower()] += 1
        if name:
            alertname_counter[name] += 1
    return {
        "open_total": len(rows),
        "by_severity": dict(severity_counter),
        "top_alertnames": alertname_counter.most_common(_TOP_ALERTS),
    }


def _slo_burn_summary(
    db: Session, team_owner: str, since_naive: datetime,
) -> Optional[Dict[str, Any]]:
    """Avg slo_burn_pct из kg_signal_aggregates за окно.

    Возвращает None если aggregates ещё не наполнились (compute_signal_aggregates
    не отработал) — секция в embed-е тогда не показывается.
    """
    rows = (
        db.query(SignalAggregate.slo_burn_pct)
        .join(Service, Service.id == SignalAggregate.service_id)
        .filter(
            Service.team_owner == team_owner,
            SignalAggregate.window_end >= since_naive,
            SignalAggregate.slo_burn_pct.isnot(None),
        )
        .all()
    )
    if not rows:
        return None
    values = [float(r.slo_burn_pct) for r in rows]
    avg = sum(values) / len(values)
    worst = max(values)
    return {
        "samples": len(values),
        "avg_burn_pct": round(avg, 2),
        "worst_burn_pct": round(worst, 2),
    }


def _top_stuck_alerts(
    db: Session, team_owner: str, limit: int = _TOP_STUCK,
) -> List[Dict[str, Any]]:
    """Top-N stuck alerts (firing >MIN_DURATION_HOURS) для команды.

    Использует общий `find_stuck_alerts` из knowledge_graph.stuck_alerts —
    тот же источник что hourly beat task, никакого double-source-of-truth.
    Сортировка — по hours_firing desc (свежий API из stuck_alerts
    возвращает поле напрямую).
    """
    min_hours = settings.STUCK_ALERTS_MIN_DURATION_HOURS
    stuck = find_stuck_alerts(db, min_duration_hours=min_hours)
    filtered = [s for s in stuck if (s.get("team_owner") == team_owner)]
    filtered.sort(key=lambda s: s.get("hours_firing", 0.0), reverse=True)
    return filtered[:limit]


def _real_service_count(db: Session, team_owner: str) -> int:
    return (
        db.query(func.count(Service.id))
        .filter(
            Service.team_owner == team_owner,
            Service.synthetic.is_(False),
        )
        .scalar()
        or 0
    )


# ────────────────────────────────────────────────────────────────────────────
# Build dict
# ────────────────────────────────────────────────────────────────────────────


def build_team_digest(
    db: Session, team_owner: str, window_hours: int = 24,
) -> Dict[str, Any]:
    """Собирает все секции digest-а в плоский dict.

    Возвращаемый dict — стабильный контракт: используется и в
    `send_team_digest` (для рендера в Discord embed) и в тестах.
    """
    since_naive = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).replace(tzinfo=None)

    fragile = _top_fragile_services(db, team_owner)
    deploys = _deploy_stats(db, team_owner, since_naive)
    alerts = _alerts_breakdown(db, team_owner, since_naive)
    slo = _slo_burn_summary(db, team_owner, since_naive)
    svc_count = _real_service_count(db, team_owner)
    stuck = _top_stuck_alerts(db, team_owner)

    return {
        "team_owner": team_owner,
        "window_hours": window_hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_count": svc_count,
        "fragile": fragile,
        "deploys": deploys,
        "alerts": alerts,
        "slo": slo,
        "stuck": stuck,
    }


# ────────────────────────────────────────────────────────────────────────────
# Render Discord embed
# ────────────────────────────────────────────────────────────────────────────


def _pick_color(digest: Dict[str, Any]) -> int:
    """Цвет embed-а по worst metric.

    Логика: open critical alerts → red, иначе если есть open warnings или
    fragile сервис с health<0.5 → yellow, иначе зелёный.
    Пустой team (нет сервисов / нет данных) → neutral.
    """
    if digest["service_count"] == 0:
        return _COLOR_NEUTRAL
    by_sev = digest["alerts"]["by_severity"]
    if by_sev.get("critical", 0) > 0:
        return _COLOR_CRITICAL
    # Любой stuck alert → red. Это эскалационный сигнал: alert тлеет >24h,
    # фактически такое же критично как «есть открытый critical».
    if digest.get("stuck"):
        return _COLOR_CRITICAL
    has_warnings = by_sev.get("warning", 0) > 0
    has_unhealthy = any(
        f["health_score"] < 0.5 for f in digest["fragile"]
    )
    if has_warnings or has_unhealthy:
        return _COLOR_WARNING
    return _COLOR_OK


def _fmt_fragile_field(fragile: List[Dict[str, Any]]) -> str:
    if not fragile:
        return "_health_score не посчитан — kg_health_recompute не наполнился_"
    lines = []
    for f in fragile:
        score = f["health_score"]
        # Bar из трёх делений: <0.34 ●○○, <0.67 ●●○, ≥0.67 ●●●. Дублирует
        # confidence_badge-семантику, чтобы Discord-читатель сразу видел шкалу.
        if score < 0.34:
            bar = "●○○"
        elif score < 0.67:
            bar = "●●○"
        else:
            bar = "●●●"
        lines.append(
            f"• `{f['name']}` _{f['namespace']}_ — {bar} `{score:.2f}`"
        )
    return "\n".join(lines)[:1024]


def _fmt_deploys_field(deploys: Dict[str, Any]) -> str:
    if deploys["total"] == 0:
        return "_нет deploys за окно_"
    pct = deploys["success_pct"]
    pct_s = f"{pct:.0f}%" if pct is not None else "?"
    return (
        f"Success rate: **{pct_s}** "
        f"({deploys['success']}/{deploys['total']})"
        + (f" · failed: `{deploys['failed']}`" if deploys["failed"] else "")
    )


def _fmt_alerts_field(alerts: Dict[str, Any]) -> str:
    if alerts["open_total"] == 0:
        return "Open: **0** — тишина"
    by_sev = alerts["by_severity"]
    sev_order = ["critical", "warning", "info", "unknown"]
    parts = [
        f"`{k}` {by_sev[k]}"
        for k in sev_order
        if by_sev.get(k)
    ]
    # Если есть severity которой не было в sev_order — добавим в хвост.
    for k, v in by_sev.items():
        if k not in sev_order and v:
            parts.append(f"`{k}` {v}")
    sev_line = ", ".join(parts) or "—"
    lines = [f"Open: **{alerts['open_total']}** · {sev_line}"]
    if alerts["top_alertnames"]:
        top = ", ".join(f"`{name}`×{cnt}" for name, cnt in alerts["top_alertnames"])
        lines.append(f"Top: {top}")
    return "\n".join(lines)[:1024]


def _fmt_stuck_field(stuck: List[Dict[str, Any]]) -> Optional[str]:
    """Top-5 stuck alerts с severity emoji + hours_firing.

    Если список пуст — возвращаем None, чтобы render_embed скрыл секцию.
    """
    if not stuck:
        return None
    lines: List[str] = []
    for s in stuck:
        hours = s.get("hours_firing", 0.0)
        emoji = severity_emoji(s.get("severity_current"), hours_firing=hours)
        svc = s.get("service") or "—"
        name = s.get("alertname") or "?"
        rec = s.get("recurrence_24h") or 0
        rec_tag = f" · 24h fires: `{rec}`" if rec > 1 else ""
        lines.append(
            f"{emoji} `{name}` _{svc}_ — **{hours:.0f}h**{rec_tag}"
        )
    return "\n".join(lines)[:1024]


def _fmt_slo_field(slo: Optional[Dict[str, Any]]) -> Optional[str]:
    if not slo:
        return None
    return (
        f"Avg burn: **{slo['avg_burn_pct']}%** · "
        f"worst: `{slo['worst_burn_pct']}%` "
        f"(samples: {slo['samples']})"
    )


def render_embed(digest: Dict[str, Any]) -> Dict[str, Any]:
    """Превратить dict из `build_team_digest` в Discord embed payload."""
    team = digest["team_owner"]
    wh = digest["window_hours"]
    color = _pick_color(digest)

    fields: List[Dict[str, Any]] = []
    fields.append({
        "name": "Services",
        "value": f"`{digest['service_count']}`",
        "inline": True,
    })
    fields.append({
        "name": "Deploys",
        "value": _fmt_deploys_field(digest["deploys"]),
        "inline": True,
    })
    fields.append({
        "name": "Alerts",
        "value": _fmt_alerts_field(digest["alerts"]),
        "inline": False,
    })
    fields.append({
        "name": f"🩺 Fragile top-{_TOP_FRAGILE}",
        "value": _fmt_fragile_field(digest["fragile"]),
        "inline": False,
    })
    stuck_value = _fmt_stuck_field(digest.get("stuck") or [])
    if stuck_value:
        fields.append({
            "name": (
                f"🔴 Stuck alerts (>{settings.STUCK_ALERTS_MIN_DURATION_HOURS}h)"
            ),
            "value": stuck_value,
            "inline": False,
        })
    slo_value = _fmt_slo_field(digest["slo"])
    if slo_value:
        fields.append({
            "name": "SLO burn (24h aggregates)",
            "value": slo_value,
            "inline": False,
        })

    embed = {
        "title": f"Daily Digest — {team} (last {wh}h)"[:256],
        "color": color,
        "fields": fields,
        "footer": {"text": f"team/{team} · window={wh}h"},
        "timestamp": digest["generated_at"],
    }
    return embed


# ────────────────────────────────────────────────────────────────────────────
# Channel mapping + send
# ────────────────────────────────────────────────────────────────────────────


def _webhook_url_for_team(team_owner: str) -> Optional[str]:
    """Резолв webhook URL для team.

    Сейчас — единственный канал `DISCORD_WEBHOOK_TEAM_DIGEST_URL`. Per-team
    mapping см. TODO в config.py (TEAM_DIGEST_CHANNEL_MAP).
    """
    url = getattr(settings, "DISCORD_WEBHOOK_TEAM_DIGEST_URL", None)
    if url:
        return url
    # Fallback на общий #stats канал — не идеал, но лучше чем drop'нуть.
    return settings.DISCORD_WEBHOOK_STATS_URL


async def _post_embed(url: str, embed: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    payload = {"embeds": [embed]}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            return False, f"status={r.status_code} body={r.text[:200]}"
        return True, None


async def send_team_digest(
    team_owner: str,
    window_hours: int = 24,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Собирает digest и шлёт в Discord. Возвращает status-dict.

    `db` — для DI в тестах. По умолчанию открываем SessionLocal и закрываем.
    """
    if not getattr(settings, "TEAM_DIGEST_ENABLED", False):
        return {"status": "skipped", "reason": "TEAM_DIGEST_ENABLED=false"}

    owned_session = False
    if db is None:
        db = SessionLocal()
        owned_session = True
    try:
        digest = build_team_digest(db, team_owner, window_hours=window_hours)
    finally:
        if owned_session:
            db.close()

    embed = render_embed(digest)

    if settings.DISCORD_DRY_RUN:
        log.info(
            "team_digest.dry_run",
            team=team_owner,
            services=digest["service_count"],
            open_alerts=digest["alerts"]["open_total"],
            fragile=len(digest["fragile"]),
        )
        return {"status": "dry_run", "team": team_owner, "digest": digest}

    url = _webhook_url_for_team(team_owner)
    if not url:
        log.warning("team_digest.no_webhook_url", team=team_owner)
        return {"status": "skipped", "reason": "no_webhook_url", "team": team_owner}

    ok, err = await _post_embed(url, embed)
    if not ok:
        log.error("team_digest.send_failed", team=team_owner, error=err)
        return {"status": "error", "team": team_owner, "error": err}
    return {"status": "sent", "team": team_owner}


# ────────────────────────────────────────────────────────────────────────────
# Beat-task entry — iterate all teams
# ────────────────────────────────────────────────────────────────────────────


def _distinct_team_owners(db: Session) -> List[str]:
    """Distinct team_owner из kg_services (real + not null)."""
    rows = db.execute(text("""
        SELECT DISTINCT team_owner
        FROM kg_services
        WHERE team_owner IS NOT NULL
          AND synthetic = false
        ORDER BY team_owner
    """)).fetchall()
    return [r[0] for r in rows if r[0]]


async def send_all_team_digests(window_hours: int = 24) -> Dict[str, Any]:
    """Beat-task entry — пройти по всем team_owner-ам и отправить digest.

    Возвращает stats: сколько teams, сколько отправлено / skipped / errored.
    """
    if not getattr(settings, "TEAM_DIGEST_ENABLED", False):
        return {"status": "skipped", "reason": "TEAM_DIGEST_ENABLED=false"}

    db = SessionLocal()
    try:
        teams = _distinct_team_owners(db)
    finally:
        db.close()

    stats: Dict[str, Any] = {
        "teams_total": len(teams),
        "sent": 0,
        "skipped": 0,
        "errors": 0,
        "results": [],
    }
    for team in teams:
        # Открываем новую сессию per team — длинная транзакция на 60+ teams
        # держит lock на kg_services дольше чем нужно.
        db = SessionLocal()
        try:
            res = await send_team_digest(team, window_hours=window_hours, db=db)
        except Exception as e:
            log.warning("team_digest.team_failed", team=team, error=str(e))
            stats["errors"] += 1
            stats["results"].append({"team": team, "status": "error", "error": str(e)})
            continue
        finally:
            db.close()
        status = res.get("status")
        if status == "sent" or status == "dry_run":
            stats["sent"] += 1
        elif status == "error":
            stats["errors"] += 1
        else:
            stats["skipped"] += 1
        stats["results"].append({"team": team, **{k: v for k, v in res.items() if k != "digest"}})

    log.info(
        "team_digest.done teams=%d sent=%d skipped=%d errors=%d",
        stats["teams_total"], stats["sent"], stats["skipped"], stats["errors"],
    )
    return stats
