"""Точка роста #2 (Phase 2): resolve-sync для kg_alerts.

Без этого taska: stale `firing` alerts от месяцев назад (etcdMembersDown
от 10 апреля) копятся вечно — `resolved_at` остаётся NULL потому что
оригинальный AM webhook resolve-сигнала не получил (или мы не обработали).

Beat task периодически:
1. GET AlertManager `/api/v2/alerts` → set активных fingerprints.
2. UPDATE kg_alerts SET resolved_at=NOW() WHERE fingerprint NOT IN (firing_set)
   AND resolved_at IS NULL AND fired_at > NOW() - INTERVAL '30 days'.
3. Fallback-проход для «фантомов» старше fallback_hours: AM не помнит их
   fingerprint (TTL/restart), окно из п.2 их выкидывает — помечаем как
   resolved=now() с маркером в raw.resolved_by='age_fallback'.

Окно 30 дней — защита от случайного восстановления свежих alerts при
hiccup AM; всё что старше — обрабатывает fallback (см. п.3), иначе stale
накапливается вечно как сейчас.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Set

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import AlertEvent

log = logging.getLogger(__name__)


async def _fetch_active_fingerprints(timeout: float = 10.0) -> Set[str]:
    """GET /api/v2/alerts с AM → set активных fingerprints.

    AM v0.28+ возвращает массив объектов с полем `fingerprint`. Если
    AM недоступен — raise; caller (beat task) ловит и no-op.
    """
    url = settings.ALERTMANAGER_API_URL.rstrip("/") + "/api/v2/alerts"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params={"active": "true", "silenced": "true"})
        r.raise_for_status()
        data = r.json()
    return {a.get("fingerprint") for a in (data or []) if a.get("fingerprint")}


def _mark_resolved(
    db: Session,
    active_fingerprints: Set[str],
    safety_min_fingerprints: int = 1,
    history_days: int = 30,
    fallback_hours: int = 24,
) -> Dict[str, int]:
    """UPDATE resolved_at для не-firing alerts.

    Два прохода:
    1) Recent (fired_at в окне history_days): fingerprint не в active →
       resolved_at=now. Safety: AM down / пустой ответ → пропуск.
    2) Age-fallback (fired_at старше fallback_hours): AM не помнит старые
       fingerprints (TTL/restart), классическое сопоставление их не
       зацепит — помечаем resolved=now с raw.resolved_by='age_fallback'.
       Идёт даже при пустом AM (active_fingerprints может быть {}),
       т.к. решение принимается по возрасту, а не по AM-снимку.
    """
    now = datetime.utcnow()
    resolved_recent = 0
    candidates_recent = 0

    if len(active_fingerprints) >= safety_min_fingerprints:
        cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
        recent = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.resolved_at.is_(None),
                AlertEvent.fired_at >= cutoff.replace(tzinfo=None),
            )
            .all()
        )
        candidates_recent = len(recent)
        for ev in recent:
            if ev.fingerprint and ev.fingerprint not in active_fingerprints:
                ev.resolved_at = now
                resolved_recent += 1

    # Fallback по возрасту — независим от safety, т.к. ориентируется
    # только на fired_at и текущий active-set (старые fingerprints из AM
    # давно вытеснены, поэтому отсутствие == правда). Окно «старше
    # fallback_hours» гарантирует что свежие алерты сюда не залетят.
    age_cutoff = datetime.utcnow() - timedelta(hours=fallback_hours)
    stale = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.resolved_at.is_(None),
            AlertEvent.fired_at < age_cutoff,
        )
        .all()
    )
    resolved_fallback = 0
    for ev in stale:
        if ev.fingerprint and ev.fingerprint in active_fingerprints:
            continue  # ещё firing в AM — не трогаем
        ev.resolved_at = now
        # Маркер для отладки / отчётности. raw — JSON, может быть None.
        raw = dict(ev.raw or {})
        raw["resolved_by"] = "age_fallback"
        raw["resolved_age_hours"] = fallback_hours
        ev.raw = raw
        resolved_fallback += 1

    total_resolved = resolved_recent + resolved_fallback
    if total_resolved:
        db.commit()
    return {
        "active_fingerprints": len(active_fingerprints),
        "candidates_open": candidates_recent,
        "resolved": total_resolved,
        "resolved_recent": resolved_recent,
        "resolved_age_fallback": resolved_fallback,
        "stale_candidates": len(stale),
    }


async def run_alerts_resolve_sync(db: Session) -> Dict[str, Any]:
    """Главная entry-point для beat task."""
    try:
        active = await _fetch_active_fingerprints()
    except Exception as e:
        log.warning("alerts_resolve_sync.fetch_failed error=%s", e)
        return {"error": str(e), "resolved": 0}

    stats = _mark_resolved(db, active)
    log.info(
        "alerts_resolve_sync.done active=%d resolved=%d recent=%d age_fallback=%d",
        stats.get("active_fingerprints", 0),
        stats.get("resolved", 0),
        stats.get("resolved_recent", 0),
        stats.get("resolved_age_fallback", 0),
    )
    return stats
