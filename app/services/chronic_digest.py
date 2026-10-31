"""L5: Chronic alerts digest для канала #stats.

Раз в N часов агрегируем kg_alerts: какие сервисы накопили ≥M fires
за последние W часов. Формируем компактный markdown-блок и шлём
DiscordService.send_stats_report — то есть в существующий #stats канал,
не плодим новые.

Цель: мьюту канала #error противодействует suppress-chronic (L2),
но мы всё равно должны знать «какие сервисы постоянно крашатся».
Этот digest и есть видимость без spam'а.

Вторая секция — «горят непрерывно»: открытые строки kg_alerts, чей эпизод
идёт дольше STUCK_ALERTS_MIN_DURATION_HOURS. Один непрерывно горящий алерт —
это ОДНА строка (fingerprint), в счёт fires выше он не попадает, а его
повторы от Alertmanager (repeat_interval) копилот в #infra-error больше не
постит (SUPPRESS_REPEAT). Без этой секции такие алерты пропали бы из виду.

Не использует LLM. Простая агрегация SQL → markdown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import AlertEvent, Service
# Порог, с которым daily-дайджест считает своё «chronic» в секции Changes.
# Импортируем, а не дублируем числом: два сообщения летят в один #stats и
# обязаны сходиться (см. stats_digest.CHRONIC_TRACKED_MIN_FIRES).
from app.services.stats_digest import CHRONIC_TRACKED_MIN_FIRES

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


# Сколько строк «горят непрерывно» показываем (остальное — счётчиком).
CONTINUOUS_SHOW_ROWS = 15
# Открытая строка, по которой AM не присылал ничего дольше этого окна, —
# скорее фантом, недобитый resolve-sync-ом, чем горящий алерт: AM повторяет
# живую группу каждые 4 ч.
CONTINUOUS_SEEN_WITHIN_HOURS = 12


def _continuous(db: Session, min_hours: int) -> List[Dict[str, Any]]:
    """Открытые алерты (resolved_at IS NULL), горящие дольше min_hours.

    LEFT JOIN на kg_services: нодовые/кластерные алерты (TooManyLogs,
    KubeVersionMismatch) сервиса не имеют, но горят ровно так же.
    """
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = (
        db.query(
            AlertEvent.alertname,
            AlertEvent.severity,
            AlertEvent.fired_at,
            AlertEvent.raw,
            Service.namespace.label("ns"),
            Service.name.label("svc"),
        )
        .outerjoin(Service, Service.id == AlertEvent.service_id)
        .filter(AlertEvent.resolved_at.is_(None))
        .filter(AlertEvent.fired_at <= now_naive - timedelta(hours=min_hours))
        .filter(
            AlertEvent.last_notified_at
            >= now_naive - timedelta(hours=CONTINUOUS_SEEN_WITHIN_HOURS)
        )
        .order_by(AlertEvent.fired_at.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        raw = r.raw if isinstance(r.raw, dict) else {}
        if r.ns and r.svc:
            where = f"{r.ns}/{r.svc}"
        else:
            where = str(raw.get("node") or raw.get("namespace") or "—")
        out.append({
            "alertname": str(r.alertname),
            "severity": str(r.severity) if r.severity else None,
            "where": where,
            "fired_at": r.fired_at,
        })
    return out


def _format_continuous(rows: List[Dict[str, Any]], min_hours: int) -> str:
    if not rows:
        return ""
    now = datetime.now(timezone.utc)
    lines = [
        f"**🔥 Горят непрерывно > {min_hours} ч** (открыты в KG, повторы "
        "Alertmanager-а в #infra-error не постятся):",
        "",
    ]
    for r in rows[:CONTINUOUS_SHOW_ROWS]:
        fired: Optional[datetime] = r["fired_at"]
        hours = 0
        if fired is not None:
            if fired.tzinfo is None:
                fired = fired.replace(tzinfo=timezone.utc)
            hours = int((now - fired).total_seconds() // 3600)
        sev = f" · {r['severity']}" if r.get("severity") else ""
        lines.append(f"• `{r['alertname']}` · `{r['where']}`{sev} · горит {hours} ч")
    if len(rows) > CONTINUOUS_SHOW_ROWS:
        lines.append(f"_(+{len(rows) - CONTINUOUS_SHOW_ROWS} ещё)_")
    return "\n".join(lines)


def _format(
    rows: List[Dict[str, Any]],
    window_hours: int,
    min_fires: int = CHRONIC_TRACKED_MIN_FIRES,
) -> str:
    if not rows:
        return ""
    now = datetime.now(timezone.utc)
    # Порог и единица группировки — в заголовке. «Chronic» встречается ещё в
    # трёх местах daily-дайджеста с другими порогами, и без подписи читатель
    # не мог сверить это число ни с одним из них (см. stats_digest,
    # CHRONIC_* константы).
    lines = [
        f"**📉 Chronic alerts** (≥{min_fires} fires/{window_hours}h "
        "по ns+сервис+alertname)",
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
    min_hours = int(settings.STUCK_ALERTS_MIN_DURATION_HOURS)
    continuous = _continuous(db, min_hours)
    if not rows and not continuous:
        log.info("chronic_digest.empty")
        return {"status": "empty", "rows": 0, "continuous": 0}

    parts = [
        _format(
            rows,
            settings.CHRONIC_DIGEST_WINDOW_HOURS,
            min_fires=settings.CHRONIC_DIGEST_MIN_FIRES,
        ),
        _format_continuous(continuous, min_hours),
    ]
    content = "\n\n".join(p for p in parts if p)
    from app.services.discord_service import DiscordService
    await DiscordService().send_stats_report(content)
    return {"status": "sent", "rows": len(rows), "continuous": len(continuous)}
