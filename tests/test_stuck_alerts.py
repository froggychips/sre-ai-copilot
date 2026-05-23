"""Тесты на app.knowledge_graph.stuck_alerts + интеграция с team_digest и
beat-task'ом kg_stuck_alerts_check.

Покрытие:
  - find_stuck_alerts: фильтрация по firing-window, resolved_at IS NULL,
    recurrence counts, orphan alerts без service_id.
  - group_by_team: 2 alert одной команды → 1 group, сортировка by count.
  - fingerprint: стабилен по порядку id-ов; разные множества → разные fp.
  - severity_emoji: bumped severity на основании hours_firing.
  - team_digest._top_stuck_alerts: фильтрация по team_owner.
  - team_digest._fmt_stuck_field: формат + None при пустом.
  - team_digest.render_embed: 5 stuck → embed содержит секцию.
  - team_digest.render_embed: 0 stuck → секции нет.
  - In-memory dedup в kg_stuck_alerts_check (двойной вызов → второй deduped).

SQLite in-memory как и test_kg_self_health.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import AlertEvent, Service
from app.knowledge_graph.stuck_alerts import (fingerprint, find_stuck_alerts,
                                              group_by_team, severity_emoji)


# ── Fixtures ──────────────────────────────────────────────────────────────


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


def _mk_service(db, name="svc-a", ns="squad-1", team="squad-1") -> Service:
    s = Service(name=name, namespace=ns, team_owner=team)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _mk_alert(
    db, svc, alertname="KubeDeploymentReplicasMismatch",
    fired_hours_ago=30.0, resolved_hours_ago=None, severity="warning",
    fingerprint_str=None,
):
    now = datetime.utcnow()
    fired_at = now - timedelta(hours=fired_hours_ago)
    resolved_at = (
        now - timedelta(hours=resolved_hours_ago)
        if resolved_hours_ago is not None
        else None
    )
    a = AlertEvent(
        service_id=svc.id if svc is not None else None,
        alertname=alertname,
        severity=severity,
        fingerprint=fingerprint_str
        or f"fp-{alertname}-{svc.id if svc else 'none'}-{fired_hours_ago:.0f}",
        fired_at=fired_at,
        resolved_at=resolved_at,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


# ── find_stuck_alerts ─────────────────────────────────────────────────────


def test_find_stuck_alerts_returns_only_firing_over_threshold(db):
    """Сценарий из задачи: 1 firing >24h, 1 firing 12h, 1 resolved (24h+
    fired_at но resolved_at set) → find_stuck_alerts(24) возвращает 1."""
    svc = _mk_service(db)

    # 1. firing >24h, не resolved → STUCK
    _mk_alert(db, svc, alertname="StuckOne", fired_hours_ago=30, severity="warning")
    # 2. firing 12h, не resolved → ниже порога
    _mk_alert(db, svc, alertname="FreshOne", fired_hours_ago=12, severity="warning")
    # 3. fired 30h назад НО resolved 1h назад → исключаем
    _mk_alert(
        db, svc, alertname="ResolvedOne",
        fired_hours_ago=30, resolved_hours_ago=1, severity="warning",
    )

    result = find_stuck_alerts(db, min_duration_hours=24)
    assert len(result) == 1
    assert result[0]["alertname"] == "StuckOne"
    assert result[0]["hours_firing"] >= 24.0
    assert result[0]["service"] == "squad-1/svc-a"
    assert result[0]["team_owner"] == "squad-1"


def test_find_stuck_alerts_includes_orphan_with_no_service(db):
    """Alert без service_id (orphan) — попадает в результат с team_owner=None."""
    _mk_alert(db, None, alertname="OrphanStuck", fired_hours_ago=40)
    result = find_stuck_alerts(db, min_duration_hours=24)
    assert len(result) == 1
    assert result[0]["team_owner"] is None
    assert result[0]["service"] == "—"


def test_find_stuck_alerts_recurrence_counts(db):
    """recurrence_24h / recurrence_7d — count тот же alertname для сервиса
    за окно."""
    svc = _mk_service(db)
    # 1 stuck (>24h)
    _mk_alert(db, svc, alertname="KubeReplicasMismatch", fired_hours_ago=30)
    # ещё 3 fired за 24h (одинаковый alertname) — recurrence
    for i in range(3):
        _mk_alert(
            db, svc, alertname="KubeReplicasMismatch",
            fired_hours_ago=2 + i, resolved_hours_ago=1,
            fingerprint_str=f"rec-24-{i}",
        )
    # ещё 2 fired за 7d (>24h, не stuck потому что resolved)
    for i in range(2):
        _mk_alert(
            db, svc, alertname="KubeReplicasMismatch",
            fired_hours_ago=72 + i, resolved_hours_ago=70 + i,
            fingerprint_str=f"rec-7d-{i}",
        )

    result = find_stuck_alerts(db, min_duration_hours=24)
    assert len(result) == 1
    # Stuck (30h) выпадает из 24h-окна; считаются только 3 свежих recurrence.
    assert result[0]["recurrence_24h"] == 3
    # За 7d: stuck (30h) + 3 свежих + 2 старых = 6
    assert result[0]["recurrence_7d"] == 6


def test_find_stuck_alerts_severity_bumped_at_48h(db):
    """severity_bumped: 30h → high, 50h → critical."""
    svc = _mk_service(db)
    _mk_alert(db, svc, alertname="A", fired_hours_ago=30, severity="warning",
              fingerprint_str="a-30")
    _mk_alert(db, svc, alertname="B", fired_hours_ago=50, severity="warning",
              fingerprint_str="b-50")
    rows = {r["alertname"]: r for r in find_stuck_alerts(db, 24)}
    assert rows["A"]["severity_bumped"] == "high"
    assert rows["B"]["severity_bumped"] == "critical"


# ── group_by_team ─────────────────────────────────────────────────────────


def test_group_by_team_collapses_same_team(db):
    """2 stuck-alert на разных сервисах одной команды → 1 entry с 2 alerts."""
    svc_a = _mk_service(db, name="svc-a", ns="squad-3", team="squad-3")
    svc_b = _mk_service(db, name="svc-b", ns="squad-3-shared", team="squad-3")
    _mk_alert(db, svc_a, alertname="A", fired_hours_ago=30, fingerprint_str="a")
    _mk_alert(db, svc_b, alertname="B", fired_hours_ago=40, fingerprint_str="b")

    stuck = find_stuck_alerts(db, 24)
    groups = group_by_team(stuck)
    assert len(groups) == 1
    g = groups[0]
    assert g.team_owner == "squad-3"
    assert g.count == 2
    # Внутри — сортировка по hours_firing desc (B был 40h, A — 30h)
    assert g.alerts[0].alertname == "B"
    assert g.alerts[1].alertname == "A"


def test_group_by_team_sorts_groups_by_count_desc(db):
    """Команда с большим числом stuck идёт первой."""
    svc_x = _mk_service(db, name="x", ns="squad-1", team="squad-1")
    svc_y = _mk_service(db, name="y", ns="squad-2", team="squad-2")
    svc_z = _mk_service(db, name="z", ns="squad-2", team="squad-2")

    _mk_alert(db, svc_x, alertname="X", fired_hours_ago=30, fingerprint_str="x")
    _mk_alert(db, svc_y, alertname="Y1", fired_hours_ago=30, fingerprint_str="y1")
    _mk_alert(db, svc_z, alertname="Y2", fired_hours_ago=30, fingerprint_str="y2")

    stuck = find_stuck_alerts(db, 24)
    groups = group_by_team(stuck)
    assert groups[0].team_owner == "squad-2"
    assert groups[0].count == 2
    assert groups[1].team_owner == "squad-1"


def test_group_by_team_unknown_for_orphan(db):
    """Orphan alert (без service) попадает в bucket 'unknown'."""
    _mk_alert(db, None, alertname="O", fired_hours_ago=30, fingerprint_str="o")
    stuck = find_stuck_alerts(db, 24)
    groups = group_by_team(stuck)
    assert len(groups) == 1
    assert groups[0].team_owner == "unknown"


# ── fingerprint ───────────────────────────────────────────────────────────


def test_fingerprint_stable_across_order():
    s1 = [{"alert_id": 5}, {"alert_id": 1}, {"alert_id": 3}]
    s2 = [{"alert_id": 3}, {"alert_id": 5}, {"alert_id": 1}]
    assert fingerprint(s1) == fingerprint(s2) == "1,3,5"


def test_fingerprint_differs_when_membership_changes():
    s1 = [{"alert_id": 1}, {"alert_id": 2}]
    s2 = [{"alert_id": 1}, {"alert_id": 3}]
    assert fingerprint(s1) != fingerprint(s2)


def test_fingerprint_empty_for_empty_list():
    assert fingerprint([]) == ""


# ── severity_emoji ────────────────────────────────────────────────────────


def test_severity_emoji_bumps_at_48h():
    """50h firing → critical → 🔴 даже если базовая severity=warning."""
    assert severity_emoji("warning", hours_firing=50.0) == "🔴"


def test_severity_emoji_bumps_at_24h():
    assert severity_emoji("warning", hours_firing=30.0) == "🟠"


def test_severity_emoji_uses_raw_when_no_hours():
    assert severity_emoji("critical") == "🔴"
    assert severity_emoji("warning") == "🟡"
    assert severity_emoji(None) == "⚪"


# ── team_digest integration ───────────────────────────────────────────────


def test_team_digest_top_stuck_alerts_filters_by_team(db):
    from app.services.team_digest import _top_stuck_alerts

    sa = _mk_service(db, name="a", ns="squad-1", team="squad-1")
    sb = _mk_service(db, name="b", ns="squad-2", team="squad-2")
    _mk_alert(db, sa, alertname="StuckA", fired_hours_ago=30, fingerprint_str="a")
    _mk_alert(db, sb, alertname="StuckB", fired_hours_ago=40, fingerprint_str="b")

    out_1 = _top_stuck_alerts(db, "squad-1")
    out_2 = _top_stuck_alerts(db, "squad-2")
    assert len(out_1) == 1 and out_1[0]["alertname"] == "StuckA"
    assert len(out_2) == 1 and out_2[0]["alertname"] == "StuckB"


def test_team_digest_fmt_stuck_field_5_alerts_text_structure(db):
    from app.services.team_digest import _fmt_stuck_field

    rows = [
        {
            "alert_id": i,
            "alertname": f"Alert{i}",
            "service": f"squad-1/svc-{i}",
            "hours_firing": 24.0 + i,
            "severity_current": "warning",
            "recurrence_24h": i + 1,
        }
        for i in range(5)
    ]
    text = _fmt_stuck_field(rows)
    assert text is not None
    # Должны быть 5 строк
    assert text.count("\n") == 4
    # Эмодзи присутствует
    assert "🟠" in text or "🔴" in text
    # Один из alert-names присутствует
    assert "Alert0" in text


def test_team_digest_fmt_stuck_field_returns_none_for_empty():
    from app.services.team_digest import _fmt_stuck_field
    assert _fmt_stuck_field([]) is None


def test_team_digest_render_embed_includes_stuck_section_when_present(db):
    from app.services.team_digest import build_team_digest, render_embed

    sa = _mk_service(db, name="a", ns="squad-1", team="squad-1")
    _mk_alert(db, sa, alertname="StuckOne", fired_hours_ago=30, fingerprint_str="a")

    digest = build_team_digest(db, "squad-1")
    embed = render_embed(digest)

    field_names = [f["name"] for f in embed["fields"]]
    assert any("Stuck alerts" in n for n in field_names), field_names


def test_team_digest_render_embed_hides_stuck_section_when_empty(db):
    from app.services.team_digest import build_team_digest, render_embed

    _mk_service(db, name="quiet", ns="squad-9", team="squad-9")

    digest = build_team_digest(db, "squad-9")
    embed = render_embed(digest)

    field_names = [f["name"] for f in embed["fields"]]
    assert not any("Stuck alerts" in n for n in field_names), field_names


# ── beat task idempotency ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kg_stuck_alerts_task_dedup_in_window(db):
    """Двойной вызов с тем же set stuck-alerts → второй раз dedup=deduped."""
    from app.workers import tasks as tasks_module

    sa = _mk_service(db, name="a", ns="squad-1", team="squad-1")
    _mk_alert(db, sa, alertname="StuckOne", fired_hours_ago=30, fingerprint_str="a")

    # Чистим state между прогонами теста
    tasks_module._STUCK_ALERTS_LAST_FIRE.clear()

    # Подменяем SessionLocal → наш in-memory session
    def session_factory():
        return db

    # Discord webhook замокаем — для теста idempotency реальный send не нужен
    mock_discord = MagicMock()
    mock_discord.send_stuck_alerts_escalation = AsyncMock(return_value=None)

    with patch.object(tasks_module, "SessionLocal", side_effect=session_factory), \
         patch("app.services.discord_service.DiscordService",
               return_value=mock_discord):
        # На in-memory session db.close() в task логике закроет нашу сессию
        # после первого вызова. Восстановим в фикстуре — обычно тестовая БД
        # — другой engine; здесь обходим, монкей-патчим db.close в no-op:
        db.close = lambda: None  # type: ignore[method-assign]

        first = await tasks_module._kg_stuck_alerts_logic()
        second = await tasks_module._kg_stuck_alerts_logic()

    assert first["status"] == "found"
    assert first["discord"] == "sent"
    assert second["discord"] == "deduped"
    # Audit-log writeable: send_stuck_alerts_escalation должен быть вызван ровно 1 раз
    assert mock_discord.send_stuck_alerts_escalation.await_count == 1
