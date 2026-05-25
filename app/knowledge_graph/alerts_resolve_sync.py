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

Регрессия 2026-04-10..2026-05-25: задача замёрзла, 16 alerts от 22 мая
остались с resolved_at IS NULL. Root cause: AM fetch raised → ранний
return без запуска fallback. Fix 2026-05-25: fallback запускается даже
при AM-failure (по возрасту — AM-снимок не нужен).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import AlertEvent

log = logging.getLogger(__name__)


async def _fetch_active_fingerprints(timeout: float = 10.0) -> Set[str]:
    """GET /api/v2/alerts с AM → set активных fingerprints.

    AM v0.28+ возвращает массив объектов с полем `fingerprint`. Если
    AM недоступен — raise; caller (beat task) ловит и запускает только
    age-fallback (alerts < age_cutoff не трогаются — те защищены окном
    history_days, а старые резолвятся по возрасту).

    Регрессия 2026-05-25: раньше дёргали только `active=true&silenced=true`,
    что в AM v2 API трактуется как exclusion-filter (inhibited / unprocessed
    выкидываются). Inhibited alerts всё ещё «реально firing» (просто
    подавлены higher-priority rule) — их фингерпринт должен быть в set,
    иначе recent-pass пометит их как resolved. Теперь включаем все 4 state.
    """
    url = settings.ALERTMANAGER_API_URL.rstrip("/") + "/api/v2/alerts"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            url,
            params={
                "active": "true",
                "silenced": "true",
                "inhibited": "true",
                "unprocessed": "true",
            },
        )
        r.raise_for_status()
        data = r.json()
    return {a.get("fingerprint") for a in (data or []) if a.get("fingerprint")}


def _mark_resolved(
    db: Session,
    active_fingerprints: Optional[Set[str]],
    safety_min_fingerprints: int = 1,
    history_days: int = 30,
    fallback_hours: int = 24,
) -> Dict[str, int]:
    """UPDATE resolved_at для не-firing alerts.

    Два прохода:
    1) Recent (fired_at в окне history_days): fingerprint не в active →
       resolved_at=now. Safety: при `active_fingerprints is None` (AM down)
       или пустом ответе — пропускаем, чтобы не зарезолвить свежие алерты
       по ошибке.
    2) Age-fallback (fired_at старше fallback_hours): AM не помнит старые
       fingerprints (TTL/restart), классическое сопоставление их не
       зацепит — помечаем resolved=now с raw.resolved_by='age_fallback'.
       Идёт даже при `active_fingerprints is None` (AM down): решение
       принимается по возрасту, а не по AM-снимку. Это safety net против
       сценария 2026-04-10 (AM unreachable → recent-pass skipped → fallback
       не запускался → freeze на 6 недель).
    """
    now = datetime.utcnow()
    resolved_recent = 0
    candidates_recent = 0

    # Recent-pass требует валидный AM-снимок (иначе risk false-resolve).
    can_run_recent = (
        active_fingerprints is not None
        and len(active_fingerprints) >= safety_min_fingerprints
    )
    if can_run_recent:
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
        # mypy: active_fingerprints проверен выше через can_run_recent.
        active_set = active_fingerprints or set()
        for ev in recent:
            if ev.fingerprint and ev.fingerprint not in active_set:
                ev.resolved_at = now
                resolved_recent += 1
        # Flush чтобы следующий SELECT в age-fallback увидел resolved_at=now
        # на уже обработанных записях (иначе double-resolve старых alerts,
        # которые попали в оба окна — recent+stale).
        if resolved_recent:
            db.flush()

    # Fallback по возрасту — независим от AM-снимка. Если active==None
    # (AM down), считаем active пустым: ничего не «защищено» AM-снимком,
    # все старые алерты резолвятся по возрасту. Это корректно потому что
    # окно fallback_hours (24h по умолчанию) уже отсекает свежие алерты.
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
    active_set_for_fallback = active_fingerprints or set()
    for ev in stale:
        if ev.fingerprint and ev.fingerprint in active_set_for_fallback:
            continue  # ещё firing в AM — не трогаем
        try:
            ev.resolved_at = now
            # Маркер для отладки / отчётности. raw — JSON, может быть None
            # или string у легаси-записей.
            raw_existing = ev.raw if isinstance(ev.raw, dict) else {}
            raw = dict(raw_existing)
            raw["resolved_by"] = "age_fallback"
            raw["resolved_age_hours"] = fallback_hours
            ev.raw = raw
            resolved_fallback += 1
        except Exception as e:
            log.warning(
                "alerts_resolve_sync.fallback_row_failed id=%s fp=%s error=%s",
                getattr(ev, "id", "?"),
                getattr(ev, "fingerprint", "?"),
                e,
            )

    total_resolved = resolved_recent + resolved_fallback
    if total_resolved:
        try:
            db.commit()
        except Exception as e:
            log.warning("alerts_resolve_sync.commit_failed error=%s", e)
            db.rollback()
            raise

    # Sample stuck fingerprints для диагностики если ничего не зарезолвили
    # но stale-кандидаты есть (классический freeze).
    stuck_sample = []
    if not total_resolved and stale:
        stuck_sample = [
            {
                "id": ev.id,
                "alertname": ev.alertname,
                "fired_at": ev.fired_at.isoformat() if ev.fired_at else None,
                "fingerprint": ev.fingerprint,
            }
            for ev in stale[:3]
        ]

    return {
        "active_fingerprints": (
            len(active_fingerprints) if active_fingerprints is not None else -1
        ),
        "candidates_open": candidates_recent,
        "resolved": total_resolved,
        "resolved_recent": resolved_recent,
        "resolved_age_fallback": resolved_fallback,
        "stale_candidates": len(stale),
        "ran_recent_pass": can_run_recent,
        "stuck_sample": stuck_sample,
    }


async def run_alerts_resolve_sync(db: Session) -> Dict[str, Any]:
    """Главная entry-point для beat task.

    Регрессия-fix 2026-05-25: при AM-failure всё равно запускаем fallback.
    Раньше ранний return при fetch-exception полностью гасил задачу —
    альерты копились вечно (см. April 10 freeze).
    """
    active: Optional[Set[str]] = None
    fetch_error: Optional[str] = None
    try:
        active = await _fetch_active_fingerprints()
    except Exception as e:
        fetch_error = str(e)
        log.warning(
            "alerts_resolve_sync.fetch_failed error=%s — продолжаем age-fallback",
            e,
        )

    stats = _mark_resolved(db, active)
    if fetch_error:
        stats["fetch_error"] = fetch_error
    log.info(
        "alerts_resolve_sync.done active=%s resolved=%d recent=%d "
        "age_fallback=%d stale=%d ran_recent=%s",
        stats.get("active_fingerprints", "?"),
        stats.get("resolved", 0),
        stats.get("resolved_recent", 0),
        stats.get("resolved_age_fallback", 0),
        stats.get("stale_candidates", 0),
        stats.get("ran_recent_pass", "?"),
    )
    return stats
