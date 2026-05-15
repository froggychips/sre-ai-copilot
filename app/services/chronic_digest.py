"""L5: Chronic alerts digest для канала #stats.

Раз в N часов агрегируем kg_alerts: какие сервисы накопили ≥M fires
за последние W часов. Формируем компактный markdown-блок и шлём
DiscordService.send_stats_report — то есть в существующий #stats канал,
не плодим новые.

Цель: мьюту канала #error противодействует suppress-chronic (L2),
но мы всё равно должны знать «какие сервисы постоянно крашатся».
Этот digest и есть видимость без spam'а.

Не использует LLM. Простая агрегация SQL → markdown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import AlertEvent, Service

log = structlog.get_logger()


def _aggregate(db: Session, window_hours: int, min_fires: int) -> List[Dict[str, Any]]:
    """SQL: group kg_alerts по (service_id, alertname), count, last_fired."""
    since_naive = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).replace(tzinfo=None)
    rows = (
        db.query(
            Service.namespace.label("ns"),
            Service.name.label("svc"),
            AlertEvent.alertname,
            func.count(AlertEvent.id).label("fires"),
            func.max(AlertEvent.fired_at).label("last_fired"),
            func.min(AlertEvent.fired_at).label("first_fired"),
        )
        .join(Service, Service.id == AlertEvent.service_id)
        .filter(AlertEvent.fired_at >= since_naive)
        .group_by(Service.namespace, Service.name, AlertEvent.alertname)
        .having(func.count(AlertEvent.id) >= min_fires)
        .order_by(func.count(AlertEvent.id).desc())
        .limit(20)
        .all()
    )
    return [
        {
            "namespace": r.ns,
            "service": r.svc,
            "alertname": r.alertname,
            "fires": int(r.fires),
            "last_fired": r.last_fired,
            "first_fired": r.first_fired,
        }
        for r in rows
    ]


def _format(rows: List[Dict[str, Any]], window_hours: int) -> str:
    if not rows:
        return ""
    now = datetime.now(timezone.utc)
    lines = [
        f"**📉 Chronic alerts (last {window_hours}h)**",
        "Эти сервисы фигурируют в #error часто — suppress-chronic скрыл повторы. Список — что фактически тлеет:",
        "",
    ]
    for r in rows[:15]:
        # Ширина «firing for» — от first_fired до now.
        first = r["first_fired"]
        if hasattr(first, "tzinfo") and first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        last = r["last_fired"]
        if hasattr(last, "tzinfo") and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        firing_hours = int((now - first).total_seconds() // 3600)
        quiet_min = int((now - last).total_seconds() // 60)
        lines.append(
            f"• `{r['namespace']}/{r['service']}` · **{r['fires']} fires** · "
            f"`{r['alertname']}` · firing {firing_hours}h · last {quiet_min}m назад"
        )
    if len(rows) > 15:
        lines.append(f"_(+{len(rows) - 15} ещё)_")
    return "\n".join(lines)


async def send_chronic_digest(db: Session) -> Dict[str, Any]:
    if not settings.CHRONIC_DIGEST_ENABLED:
        return {"status": "skipped", "reason": "CHRONIC_DIGEST_ENABLED=false"}
    rows = _aggregate(
        db,
        window_hours=settings.CHRONIC_DIGEST_WINDOW_HOURS,
        min_fires=settings.CHRONIC_DIGEST_MIN_FIRES,
    )
    if not rows:
        log.info("chronic_digest.empty")
        return {"status": "empty", "rows": 0}

    content = _format(rows, settings.CHRONIC_DIGEST_WINDOW_HOURS)
    from app.services.discord_service import DiscordService
    await DiscordService().send_stats_report(content)
    return {"status": "sent", "rows": len(rows)}
