"""Тесты на kg_alerts_resolve_sync — beat task для resolved_at refresh.

Покрытие:
1. Recent-pass: AM знает живой fingerprint → не резолвит; не знает → резолвит.
2. Recent-pass safety: пустой AM-снимок → не запускается (риск false-resolve).
3. Age-fallback: всегда работает, режет alerts старше 24h.
4. Регрессия 2026-04-10: AM-fetch failure НЕ блокирует age-fallback (раньше
   ранний return гасил всё; fix 2026-05-25).
5. Per-row error tolerance в fallback.
6. backfill_resolved_alerts CLI:
   - dry-run не пишет
   - apply резолвит подходящих
   - --no-am пропускает AM-проверку
   - идемпотентность

SQLite in-memory как и test_kg_self_health.py.
"""
from datetime import datetime, timedelta
from typing import Set
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.alerts_resolve_sync import (
    _mark_resolved,
    run_alerts_resolve_sync,
)
from app.knowledge_graph.schema import AlertEvent, Service
from app.scripts.backfill_resolved_alerts import (
    BACKFILL_MARKER,
    apply_backfill,
    plan_backfill,
    run_backfill_resolved,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk_alert(
    db,
    *,
    alertname: str = "X",
    fingerprint: str = "fp-x",
    fired_at: datetime = None,
    resolved_at=None,
    raw=None,
) -> AlertEvent:
    ev = AlertEvent(
        alertname=alertname,
        fingerprint=fingerprint,
        fired_at=fired_at or datetime.utcnow(),
        resolved_at=resolved_at,
        raw=raw,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


# ── _mark_resolved: recent-pass ───────────────────────────────────────────


def test_recent_pass_resolves_fingerprints_not_in_active(db):
    """fingerprint не в active → recent-pass резолвит."""
    fresh = datetime.utcnow() - timedelta(hours=2)
    a1 = _mk_alert(db, fingerprint="fp-1", fired_at=fresh)
    a2 = _mk_alert(db, fingerprint="fp-2", fired_at=fresh)

    stats = _mark_resolved(db, active_fingerprints={"fp-1"})
    db.refresh(a1)
    db.refresh(a2)

    # fp-1 ещё живой → не трогаем; fp-2 нет в AM → резолв.
    assert a1.resolved_at is None
    assert a2.resolved_at is not None
    assert stats["resolved_recent"] == 1
    assert stats["ran_recent_pass"] is True


def test_recent_pass_skipped_on_empty_am(db):
    """Пустой AM-снимок → recent-pass пропущен (safety от false-resolve).

    Свежий fingerprint остаётся NULL — пока он не выпадает в age-fallback.
    """
    fresh = datetime.utcnow() - timedelta(hours=2)
    a1 = _mk_alert(db, fingerprint="fp-1", fired_at=fresh)

    stats = _mark_resolved(db, active_fingerprints=set())
    db.refresh(a1)

    assert a1.resolved_at is None  # не зарезолвили
    assert stats["resolved_recent"] == 0
    assert stats["ran_recent_pass"] is False


# ── _mark_resolved: age-fallback ──────────────────────────────────────────


def test_fallback_resolves_old_alerts_when_am_empty(db):
    """Alerts старше 24h резолвятся через fallback при пустом AM-снимке.

    Recent-pass пропущен (active пуст < safety_min), fallback всё равно
    режет старые. Маркер `raw.resolved_by='age_fallback'` записан.
    """
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    stats = _mark_resolved(db, active_fingerprints=set())
    db.refresh(a1)

    assert a1.resolved_at is not None
    assert stats["resolved_age_fallback"] == 1
    assert stats["resolved_recent"] == 0
    # Маркер для отладки записан.
    assert a1.raw is not None
    assert a1.raw.get("resolved_by") == "age_fallback"


def test_recent_pass_handles_old_alert_before_fallback(db):
    """Когда recent-pass работает: старый alert не в AM → recent резолвит первым.

    Fallback не должен дублить (flush после recent делает запись невидимой
    для fallback-query через `resolved_at IS NULL`).
    """
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    # AM знает unrelated → recent-pass запускается (>= safety_min), резолвит
    # fp-old первым; fallback видит 0 stale.
    stats = _mark_resolved(db, active_fingerprints={"unrelated-fp"})
    db.refresh(a1)

    assert a1.resolved_at is not None
    assert stats["resolved_recent"] == 1
    assert stats["resolved_age_fallback"] == 0
    assert stats["resolved"] == 1  # без double-count


def test_fallback_skips_still_firing_in_am(db):
    """Если fingerprint всё ещё в AM — fallback не трогает (даже если old)."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-still-firing", fired_at=old)

    stats = _mark_resolved(db, active_fingerprints={"fp-still-firing"})
    db.refresh(a1)

    assert a1.resolved_at is None
    assert stats["resolved_age_fallback"] == 0


def test_fallback_runs_when_active_is_none(db):
    """РЕГРЕССИЯ 2026-04-10: AM-fetch failed → active=None → fallback всё
    равно должен работать."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    stats = _mark_resolved(db, active_fingerprints=None)
    db.refresh(a1)

    assert a1.resolved_at is not None
    assert stats["resolved_age_fallback"] == 1
    assert stats["ran_recent_pass"] is False
    # active_fingerprints=None помечается как -1 для отчётности.
    assert stats["active_fingerprints"] == -1


def test_fallback_resolves_null_fingerprint_alerts(db):
    """Legacy-записи без fingerprint (NULL) — fallback всё равно резолвит."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint=None, fired_at=old)

    stats = _mark_resolved(db, active_fingerprints={"unrelated"})
    db.refresh(a1)

    assert a1.resolved_at is not None
    assert stats["resolved_age_fallback"] == 1


def test_fallback_preserves_existing_raw_keys(db):
    """raw.resolved_by добавляется, существующие ключи сохраняются."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(
        db,
        fingerprint="fp-old",
        fired_at=old,
        raw={"description": "etcd is dying", "team": "platform"},
    )

    _mark_resolved(db, active_fingerprints=set())
    db.refresh(a1)

    assert a1.raw is not None
    assert a1.raw["description"] == "etcd is dying"
    assert a1.raw["team"] == "platform"
    assert a1.raw["resolved_by"] == "age_fallback"


def test_fallback_tolerates_non_dict_raw(db):
    """Legacy-запись с raw как строкой / list не должна крашить fallback."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)
    # Подсовываем «грязный» raw — некоторые legacy-БД могли сохранить строку.
    a1.raw = "not a dict"  # type: ignore[assignment]
    db.commit()

    stats = _mark_resolved(db, active_fingerprints=set())
    db.refresh(a1)

    # resolved_at всё равно проставлен; raw перезаписан валидным dict.
    assert a1.resolved_at is not None
    assert isinstance(a1.raw, dict)
    assert a1.raw["resolved_by"] == "age_fallback"
    assert stats["resolved_age_fallback"] == 1


def test_stuck_sample_emitted_when_freeze(db):
    """Если есть stale-кандидаты но ничего не зарезолвили — sample в логе."""
    old = datetime.utcnow() - timedelta(hours=48)
    _mk_alert(db, fingerprint="fp-still-1", fired_at=old)
    _mk_alert(db, fingerprint="fp-still-2", fired_at=old)
    _mk_alert(db, fingerprint="fp-still-3", fired_at=old)

    stats = _mark_resolved(
        db, active_fingerprints={"fp-still-1", "fp-still-2", "fp-still-3"}
    )

    # Ничего не зарезолвили, но stale_candidates>0 → stuck_sample непуст.
    assert stats["resolved"] == 0
    assert stats["stale_candidates"] == 3
    assert len(stats["stuck_sample"]) > 0


# ── run_alerts_resolve_sync: integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_with_am_failure_still_resolves_old(db):
    """РЕГРЕССИЯ-fix: AM unreachable → продолжаем fallback, резолвим >24h."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    async def _boom(timeout=10.0):
        raise ConnectionError("AM unreachable from worker")

    with patch(
        "app.knowledge_graph.alerts_resolve_sync._fetch_active_fingerprints",
        side_effect=_boom,
    ):
        stats = await run_alerts_resolve_sync(db)

    db.refresh(a1)
    assert a1.resolved_at is not None
    assert stats["resolved"] == 1
    assert stats["resolved_age_fallback"] == 1
    assert "fetch_error" in stats
    assert "AM unreachable" in stats["fetch_error"]


@pytest.mark.asyncio
async def test_run_with_am_success_resolves_both_passes(db):
    """Happy path: AM знает живой fingerprint, мы знаем стале — оба прохода."""
    fresh = datetime.utcnow() - timedelta(hours=2)
    old = datetime.utcnow() - timedelta(hours=48)
    a_fresh_dead = _mk_alert(db, fingerprint="fp-fresh-dead", fired_at=fresh)
    a_fresh_live = _mk_alert(db, fingerprint="fp-fresh-live", fired_at=fresh)
    a_old = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    async def _active(timeout=10.0) -> Set[str]:
        # live в AM остался только один.
        return {"fp-fresh-live"}

    with patch(
        "app.knowledge_graph.alerts_resolve_sync._fetch_active_fingerprints",
        side_effect=_active,
    ):
        stats = await run_alerts_resolve_sync(db)

    db.refresh(a_fresh_dead)
    db.refresh(a_fresh_live)
    db.refresh(a_old)

    assert a_fresh_live.resolved_at is None
    assert a_fresh_dead.resolved_at is not None  # recent-pass
    assert a_old.resolved_at is not None  # recent-pass (он < 30d, попал сюда первым)
    # Оба резолва идут через recent-pass (fired_at < 30d). Fallback пуст,
    # т.к. recent flush делает старый alert невидимым для stale-query.
    assert stats["resolved"] == 2
    assert stats["resolved_recent"] == 2
    assert stats["resolved_age_fallback"] == 0


# ── backfill_resolved_alerts ──────────────────────────────────────────────


def test_backfill_dry_run_does_not_write(db):
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    result = run_backfill_resolved(
        db, apply=False, min_age_hours=1.0, use_am=False
    )
    db.refresh(a1)

    assert a1.resolved_at is None  # dry-run
    assert result.would_resolve == 1
    assert result.actually_resolved == 0


def test_backfill_apply_resolves(db):
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-old", fired_at=old)

    result = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False
    )
    db.refresh(a1)

    assert a1.resolved_at is not None
    assert result.actually_resolved == 1
    assert a1.raw is not None
    assert a1.raw["resolved_by"] == BACKFILL_MARKER


def test_backfill_skips_too_young(db):
    """alerts моложе min_age_hours — не трогаем (защита от race)."""
    young = datetime.utcnow() - timedelta(minutes=30)
    a1 = _mk_alert(db, fingerprint="fp-young", fired_at=young)

    result = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False
    )
    db.refresh(a1)

    assert a1.resolved_at is None
    assert result.skipped_too_young == 1


def test_backfill_skips_still_firing_when_am_used(db):
    """Если AM знает fingerprint → не резолвим."""
    old = datetime.utcnow() - timedelta(hours=48)
    a1 = _mk_alert(db, fingerprint="fp-still", fired_at=old)
    a2 = _mk_alert(db, fingerprint="fp-dead", fired_at=old)

    # Передаём active set вручную через plan_backfill (минуя AM).
    plan, result = plan_backfill(
        db,
        active_fingerprints={"fp-still"},
        min_age_hours=1.0,
    )
    apply_backfill(db, plan)
    db.refresh(a1)
    db.refresh(a2)

    assert a1.resolved_at is None  # ещё живой в AM
    assert a2.resolved_at is not None
    assert result.skipped_still_firing == 1
    assert result.would_resolve == 1


def test_backfill_idempotent_on_second_run(db):
    old = datetime.utcnow() - timedelta(hours=48)
    _mk_alert(db, fingerprint="fp-old", fired_at=old)

    r1 = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False
    )
    r2 = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False
    )

    assert r1.actually_resolved == 1
    # На втором проходе уже всё закрыто.
    assert r2.actually_resolved == 0
    assert r2.would_resolve == 0
    assert r2.open_total == 0


def test_backfill_limit_caps_batch(db):
    old = datetime.utcnow() - timedelta(hours=48)
    for i in range(5):
        _mk_alert(db, fingerprint=f"fp-old-{i}", fired_at=old)

    result = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False, limit=2
    )

    assert result.actually_resolved == 2
    assert result.open_total == 5  # видим все


def test_backfill_no_am_flag_resolves_all_old(db):
    """--no-am: пропускаем AM, резолвим всё что старше min-age-hours."""
    old = datetime.utcnow() - timedelta(hours=48)
    _mk_alert(db, fingerprint="fp-1", fired_at=old)
    _mk_alert(db, fingerprint="fp-2", fired_at=old)

    result = run_backfill_resolved(
        db, apply=True, min_age_hours=1.0, use_am=False
    )

    assert result.actually_resolved == 2
    assert result.am_active_count == -1
