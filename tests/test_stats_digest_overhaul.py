"""Тесты для stats_digest overhaul 2026-05-25.

Покрытие:
  * A1. Δ-only digest — `_compute_change_report` / `changes_section` с/без cache.
  * A2. Skip-if-noop в `send_daily_digest`.
  * A3. Per-team strict scope в team_digest.
  * B4. Action items (chronic / unowned / suspicious_stale).
  * B5. Top noisemakers.
  * B6. MTTR mini-stat.
  * B7. Deploy-incident correlation matcher (within 30m).
  * B8. Topology growth diff.
  * C9. Pipeline health gauge with stale.
  * C10. Beat-task heartbeats footer.
  * Clickable TC build URLs в Recent deploys.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import stats_digest
from app.services.stats_digest import ChangeReport


# ── A1. Δ-only digest: ChangeReport + changes_section ──────────────────────


def test_change_report_new_baseline_when_no_snapshot():
    """previous=None → new_baseline=True."""
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 0
    import asyncio
    report = asyncio.run(stats_digest._compute_change_report(
        db, firing_today=10, crashloops_today=2, previous=None,
    ))
    assert report.new_baseline is True
    assert report.firing_series_today == 10


def test_change_report_with_previous_computes_deltas():
    """previous=dict → deltas заполнены."""
    db = MagicMock()
    # Order: fired (24h), resolved (24h), chronic (24h), edges, services
    db.execute.return_value.scalar.side_effect = [
        15,   # fired_in_window
        7,    # resolved_in_window
        3,    # chronic
        1500, # edges
        300,  # services
    ]
    prev = {
        "firing_series": 100,
        "crashloops": 5,
        "kg_edges": 1450,
        "kg_services": 290,
    }
    import asyncio
    report = asyncio.run(stats_digest._compute_change_report(
        db, firing_today=120, crashloops_today=3, previous=prev,
    ))
    assert report.new_baseline is False
    assert report.firing_series_today == 120
    assert report.firing_series_yesterday == 100
    assert report.kg_edges_today == 1500
    assert report.kg_edges_yesterday == 1450


def test_changes_section_renders_new_baseline_pill():
    report = ChangeReport(
        new_baseline=True,
        new_alerts_24h=12,
        resolved_alerts_24h=4,
        kg_edges_today=1500,
    )
    text = stats_digest.changes_section(report)
    assert "Changes since yesterday" in text
    assert "new baseline" in text
    assert "`12` new alerts" in text


def test_changes_section_renders_full_delta():
    report = ChangeReport(
        new_baseline=False,
        new_alerts_24h=12,
        chronic_in_new=5,
        resolved_alerts_24h=8,
        kg_edges_today=1500,
        kg_edges_yesterday=1453,
    )
    text = stats_digest.changes_section(report)
    assert "+12` new alerts (5 chronic)" in text
    assert "-8` resolved" in text
    assert "+47` KG edges" in text


# ── A2. Skip-if-noop ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_daily_digest_skipped_noop():
    """Все секции пустые + 0 alerts → status=skipped_noop, send не вызывается."""
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock()
    empty_meta = {
        "sections_with_content": 0,
        "change_report": ChangeReport(new_baseline=True),
        "fired_series_count": 0,
    }
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", True, create=True), \
         patch.object(
             stats_digest, "_build_digest_with_meta",
             new=AsyncMock(return_value=("DIGEST_BODY", empty_meta)),
         ), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result["status"] == "skipped_noop"
    fake_discord.send_stats_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_daily_digest_no_skip_when_alerts_present():
    """Если есть new alerts даже когда секции пустые — post идёт."""
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock()
    meta = {
        "sections_with_content": 0,
        "change_report": ChangeReport(new_alerts_24h=5),
        "fired_series_count": 0,
    }
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", True, create=True), \
         patch.object(
             stats_digest, "_build_digest_with_meta",
             new=AsyncMock(return_value=("BODY", meta)),
         ), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result["status"] == "sent"
    fake_discord.send_stats_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_daily_digest_no_skip_when_disabled_setting():
    """Если STATS_DIGEST_SKIP_NOOP=False — постим даже на noop."""
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock()
    empty_meta = {
        "sections_with_content": 0,
        "change_report": ChangeReport(new_baseline=True),
        "fired_series_count": 0,
    }
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", False, create=True), \
         patch.object(
             stats_digest, "_build_digest_with_meta",
             new=AsyncMock(return_value=("BODY", empty_meta)),
         ), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result["status"] == "sent"


# ── A3. Per-team strict scope (team_digest) ────────────────────────────────


def test_team_digest_queries_have_team_owner_filter():
    """Все SQL-функции team_digest должны фильтровать по team_owner.

    Грепом проверяем: каждая aggregation-функция в team_digest содержит
    `team_owner ==` или `team_owner=` в SQL.
    """
    import inspect
    from app.services import team_digest

    aggregators = [
        team_digest._top_fragile_services,
        team_digest._deploy_stats,
        team_digest._alerts_breakdown,
        team_digest._slo_burn_summary,
        team_digest._real_service_count,
    ]
    for fn in aggregators:
        src = inspect.getsource(fn)
        assert "team_owner ==" in src or "team_owner =" in src, (
            f"{fn.__name__} лишён team_owner-фильтра"
        )


# ── B4. Action items ───────────────────────────────────────────────────────


def test_chronic_action_items_returns_top_3():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("clickhouse-keeper", "OOMKilled", 25),
        ("bot-service", "CrashLooping", 18),
        ("map-service", "RestartLoop", 12),
    ]
    items = stats_digest._chronic_action_items(db, threshold=10)
    assert len(items) == 3
    assert items[0]["service"] == "clickhouse-keeper"
    assert items[0]["fires"] == 25


def test_action_items_section_renders_three_categories():
    db = MagicMock()
    # scalar() для unowned/stale, fetchall() для chronic
    db.execute.return_value.fetchall.return_value = [
        ("svc-a", "AlertX", 15),
    ]
    db.execute.return_value.scalar.side_effect = [5, 7]
    text = stats_digest.action_items_section(db, chronic_threshold=10)
    assert "Action items" in text
    assert "chronic alerts" in text
    assert "RCA: svc-a" in text
    assert "without_owner_count_5" not in text  # sanity
    assert "5` services без owner" in text
    assert "7` suspicious_stale" in text


def test_action_items_section_empty_returns_empty_string():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.side_effect = [0, 0]
    text = stats_digest.action_items_section(db)
    assert text == ""


# ── B5. Noisemakers ────────────────────────────────────────────────────────


def test_noisemakers_section_shows_dominant_service():
    """Один сервис генерирует ≥20% алертов → попадает в список."""
    fired = (
        [{"metric": {"namespace": "ns1", "service": "clickhouse-keeper"}}] * 38
        + [{"metric": {"namespace": "ns2", "service": "bot-service"}}] * 21
        + [{"metric": {"namespace": "ns3", "service": "other"}}] * 41
    )
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)
    assert "Noisemakers" in text
    assert "clickhouse-keeper" in text
    # other набирает 41% — тоже в списке
    assert "other" in text


def test_noisemakers_section_hides_when_no_dominant():
    """Все сервисы равномерно — секция скрыта."""
    fired = [
        {"metric": {"namespace": "ns1", "service": f"svc-{i}"}}
        for i in range(20)
    ]  # каждый 5%
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)
    assert text == ""


def test_noisemakers_section_hides_when_empty():
    text = stats_digest.noisemakers_section([], threshold_pct=20.0)
    assert text == ""


# ── B6. MTTR ───────────────────────────────────────────────────────────────


def test_mttr_section_renders_when_resolved_alerts():
    db = MagicMock()
    # _mttr_stats called twice (days, days*2)
    db.execute.return_value.fetchone.side_effect = [
        (8.0, 47.0, 42),  # current 7d
        (10.0, 60.0, 80),  # prev (combined 14d)
    ]
    text = stats_digest.mttr_section(db, days=7)
    assert "MTTR" in text
    assert "8min" in text
    assert "47min" in text
    assert "42" in text  # samples


def test_mttr_section_hidden_when_no_samples():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (None, None, 0)
    text = stats_digest.mttr_section(db, days=7)
    assert text == ""


def test_mttr_section_graceful_on_missing_table():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("relation does not exist")
    text = stats_digest.mttr_section(db, days=7)
    assert text == ""


# ── B7. Deploy-incident correlation ────────────────────────────────────────


def test_deploy_incident_correlation_renders():
    """Базовый ренdere: 18 deploys, 7 attributed, worst=Build#2138 by wizaryx."""
    db = MagicMock()
    # overall fetchone + worst fetchone
    db.execute.return_value.fetchone.side_effect = [
        (18, 7, 11),  # total, attributed, successes
        ("2138", "wizaryx", 3),  # worst
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Deploy" in text
    assert "incident correlation" in text
    assert "18" in text
    assert "7" in text
    assert "Build #2138" in text
    assert "wizaryx" in text


def test_deploy_incident_correlation_hides_when_no_deploys():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (0, 0, 0)
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert text == ""


def test_deploy_incident_correlation_skips_worst_when_below_threshold():
    """Worst показываем только при ≥2 alerts."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (10, 1, 9),
        ("2200", "user1", 1),  # 1 alert — игнорируем worst
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Worst" not in text


# ── B8. Topology growth ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topology_growth_section_renders_diff():
    """previous snapshot есть → Δ рисуется."""
    db = MagicMock()
    db.execute.return_value.scalar.side_effect = [300, 1500]
    db.execute.return_value.fetchall.return_value = [
        ("events.refresh",), ("push.queue",), ("billing.events",),
    ]

    fake_prev = {
        "services": 288,
        "edges": 1453,
        "nats_subjects": [],
    }
    with patch.object(
        stats_digest, "_read_topology_snapshot",
        new=AsyncMock(return_value=fake_prev),
    ), patch.object(
        stats_digest, "_write_topology_snapshot",
        new=AsyncMock(),
    ):
        text = await stats_digest.topology_growth_section(db)
    assert "Topology growth" in text
    assert "`+12` services" in text
    assert "`+47` edges" in text
    assert "NATS subjects" in text


@pytest.mark.asyncio
async def test_topology_growth_section_hidden_on_first_run():
    """previous=None → секция скрыта, но snapshot записан."""
    db = MagicMock()
    db.execute.return_value.scalar.side_effect = [300, 1500]
    db.execute.return_value.fetchall.return_value = []

    write_mock = AsyncMock()
    with patch.object(stats_digest, "_read_topology_snapshot",
                      new=AsyncMock(return_value=None)), \
         patch.object(stats_digest, "_write_topology_snapshot", new=write_mock):
        text = await stats_digest.topology_growth_section(db)
    assert text == ""
    write_mock.assert_awaited_once()  # snapshot всё равно сохранён


# ── C9. Pipeline health ────────────────────────────────────────────────────


def test_pipeline_health_section_marks_stale_when_lag_exceeds_threshold():
    from app.knowledge_graph.self_health import CheckResult
    fake_result = CheckResult(
        name="sync_lag",
        status="warn",
        detail={"per_task": {
            "kg_metrics_sync": {"lag_minutes": 5.0, "status": "ok",
                                "last_ts": "2026-05-25T12:00:00", "expected_interval_minutes": 10},
            "kg_seq_logs_sync": {"lag_minutes": 90.0, "status": "warn",
                                 "last_ts": "2026-05-25T10:30:00", "expected_interval_minutes": 10},
        }},
    )
    db = MagicMock()
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=fake_result):
        text = stats_digest.pipeline_health_section(db, stale_minutes=60)
    assert "Pipeline" in text
    assert "vmsingle ✓" in text
    assert "seq ⚠️" in text
    assert "gap" in text  # 90 min ≈ 2h (rounded)


def test_pipeline_health_section_graceful_on_check_error():
    db = MagicMock()
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               side_effect=RuntimeError("DB down")):
        text = stats_digest.pipeline_health_section(db)
    assert text == ""


# ── C10. Beat heartbeats footer ────────────────────────────────────────────


def test_beat_heartbeats_footer_renders_lag():
    from app.knowledge_graph.self_health import CheckResult
    fake = CheckResult(
        name="sync_lag",
        status="ok",
        detail={"per_task": {
            "kg_metrics_sync": {"lag_minutes": 5.0, "status": "ok",
                                "last_ts": "2026-05-25T14:45:00", "expected_interval_minutes": 10},
            "kg_topology_sync": {"lag_minutes": 320.0, "status": "warn",
                                 "last_ts": "2026-05-25T12:17:00", "expected_interval_minutes": 60},
        }},
    )
    db = MagicMock()
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               return_value=fake):
        text = stats_digest.beat_heartbeats_footer(db)
    assert "Syncs:" in text
    assert "metrics 14:45" in text
    assert "topology 12:17" in text
    assert "5h ago" in text  # 320 min → 5h


def test_beat_heartbeats_footer_graceful_on_error():
    db = MagicMock()
    with patch("app.knowledge_graph.self_health.check_sync_lag",
               side_effect=RuntimeError("boom")):
        text = stats_digest.beat_heartbeats_footer(db)
    assert text == ""


# ── Clickable TC build URLs ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_deploys_includes_clickable_url_for_single_build():
    """Single build с id → Markdown link [Build and update (...)](URL)."""
    async def fake_fetch(*, lookback_hours, limit):
        return [{
            "id": 125133, "number": "2138", "status": "SUCCESS",
            "branch": "refs/heads/preprod",
            "buildtype_name": "Build and update",
            "finished_at": "2026-05-24T20:00:00+00:00",
            "triggered_by": "wizaryx", "triggered_type": "user",
        }]
    with patch.object(stats_digest.settings, "TEAMCITY_WEB_URL",
                      "https://wo-teamcity.lastoasisgame.com", create=True):
        text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "[Build and update" in text
    assert "viewLog.html?buildId=125133" in text
    assert "](https://wo-teamcity.lastoasisgame.com" in text


@pytest.mark.asyncio
async def test_recent_deploys_includes_url_for_cascade():
    """Cascade aggregation тоже получает clickable header."""
    async def fake_fetch(*, lookback_hours, limit):
        return [
            {"id": 100, "number": "2138", "status": "SUCCESS",
             "branch": "refs/heads/preprod-kingdom2",
             "buildtype_name": "town-service",
             "finished_at": "2026-05-24T20:00:00+00:00",
             "triggered_by": "wizaryx", "triggered_type": "user"},
            {"id": 101, "number": "2138", "status": "SUCCESS",
             "branch": "refs/heads/preprod-kingdom2",
             "buildtype_name": "chat-tasks",
             "finished_at": "2026-05-24T20:00:00+00:00",
             "triggered_by": "wizaryx", "triggered_type": "user"},
        ]
    with patch.object(stats_digest.settings, "TEAMCITY_WEB_URL",
                      "https://wo-teamcity.lastoasisgame.com", create=True):
        text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    # Wrapped link приоритетно вокруг "#2138 by wizaryx"
    assert "[#2138 by `wizaryx`]" in text
    assert "buildId=100" in text  # первый build_id из cascade


@pytest.mark.asyncio
async def test_recent_deploys_no_url_when_id_missing():
    """build без id → plain text, без ломанной ссылки."""
    async def fake_fetch(*, lookback_hours, limit):
        return [{
            "number": "500", "status": "SUCCESS",
            "branch": "refs/heads/preprod",
            "buildtype_name": "auth-service",
            "finished_at": "2026-05-24T20:00:00+00:00",
            "triggered_by": "u1", "triggered_type": "user",
            # NO id field
        }]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    # Никаких лом-link [...](None) или [...]()
    assert "](" not in text or "viewLog.html" in text
    # plain text rendered
    assert "auth-service" in text


# ── TC URL prefix helper ───────────────────────────────────────────────────


def test_tc_url_prefix_strips_trailing_slash():
    with patch.object(stats_digest.settings, "TEAMCITY_WEB_URL",
                      "https://wo-teamcity.lastoasisgame.com/", create=True):
        assert stats_digest._tc_url_prefix() == \
            "https://wo-teamcity.lastoasisgame.com"


def test_tc_url_prefix_default_when_unset():
    with patch.object(stats_digest.settings, "TEAMCITY_WEB_URL", "", create=True), \
         patch.object(stats_digest.settings, "TC_URL_PREFIX", "", create=True):
        # default fallback в коде
        assert stats_digest._tc_url_prefix() == \
            "https://wo-teamcity.lastoasisgame.com"


# ── Day snapshot helpers ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_day_snapshot_roundtrip():
    """Запись → чтение возвращает тот же dict."""
    fake_client = MagicMock()
    stored = {}

    async def _set(key, value, ex=None):
        stored[key] = value

    async def _get(key):
        return stored.get(key)

    fake_client.set = _set
    fake_client.get = _get
    with patch("app.services.alert_dedup._get_client", return_value=fake_client):
        await stats_digest._write_day_snapshot({"firing_series": 100, "kg_edges": 1500})
        result = await stats_digest._read_day_snapshot()
    assert result == {"firing_series": 100, "kg_edges": 1500}


@pytest.mark.asyncio
async def test_day_snapshot_read_returns_none_when_missing():
    fake_client = MagicMock()

    async def _get(key):
        return None

    fake_client.get = _get
    with patch("app.services.alert_dedup._get_client", return_value=fake_client):
        result = await stats_digest._read_day_snapshot()
    assert result is None


# ── Topology helpers ───────────────────────────────────────────────────────


def test_nats_subjects_extraction_from_edges():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("events.refresh",), ("push.queue",), ("billing.events",),
    ]
    subjects = stats_digest._nats_subjects(db)
    assert subjects == ["billing.events", "events.refresh", "push.queue"]


def test_count_alerts_in_window_graceful():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("no table")
    fired, resolved = stats_digest._count_alerts_in_window(db, hours=24)
    assert fired == 0
    assert resolved == 0
