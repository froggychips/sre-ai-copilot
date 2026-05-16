"""Точка роста #2 (Phase 2): resolve-sync для kg_alerts.

Без этого taska: stale `firing` alerts от месяцев назад (etcdMembersDown
от 10 апреля) копятся вечно — `resolved_at` остаётся NULL потому что
оригинальный AM webhook resolve-сигнала не получил (или мы не обработали).

Beat task периодически:
1. GET AlertManager `/api/v2/alerts` → set активных fingerprints.
2. UPDATE kg_alerts SET resolved_at=NOW() WHERE fingerprint NOT IN (firing_set)
   AND resolved_at IS NULL AND fired_at > NOW() - INTERVAL '30 days'.

Окно 30 дней — защита от случайного восстановления старых alerts если
AM пустой (e.g., временный hiccup); resolved через 30 дней считается
historically resolved независимо.
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
) -> Dict[str, int]:
    """UPDATE resolved_at для не-firing alerts.

    Safety: если AM вернул < safety_min_fingerprints (e.g., пустой при
    AM down) — не помечать массово. Реалистично: даже на чистом кластере
    Watchdog alert всегда firing.
    """
    if len(active_fingerprints) < safety_min_fingerprints:
        return {"skipped_low_fingerprints": len(active_fingerprints), "resolved": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
    candidates = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.resolved_at.is_(None),
            AlertEvent.fired_at >= cutoff.replace(tzinfo=None),
        )
        .all()
    )
    resolved = 0
    now = datetime.utcnow()
    for ev in candidates:
        if ev.fingerprint and ev.fingerprint not in active_fingerprints:
            ev.resolved_at = now
            resolved += 1
    if resolved:
        db.commit()
    return {
        "active_fingerprints": len(active_fingerprints),
        "candidates_open": len(candidates),
        "resolved": resolved,
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
        "alerts_resolve_sync.done active=%d resolved=%d",
        stats.get("active_fingerprints", 0), stats.get("resolved", 0),
    )
    return stats
