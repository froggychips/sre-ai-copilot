"""Тесты для A2 (severity decay) + B4 (similar past incident lookup).

Покрывают:
  * _age_decay_severity: critical > 24h без ack → stale; critical < 24h → no-op;
    warning > 24h → no-op (decay только для critical); acked critical → no-op.
  * _decay_color: маппинг stale_critical → orange, остальное — без изменений.
  * _build_similar_past_field: рендеринг с deploy + без deploy + missing data.
  * send_incident_report: end-to-end что title-prefix/color/footer применяются
    когда передан fired_at старше 24h, и similar past field добавляется когда
    lookup возвращает hit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# A2: severity decay
# ---------------------------------------------------------------------------


def test_decay_critical_over_24h_unacked_returns_stale():
    from app.services.discord_service import _age_decay_severity

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fired_at = now - timedelta(hours=25)
    sev, prefix, marker = _age_decay_severity("critical", fired_at, acked_by=None, now=now)
    assert sev == "stale_critical"
    assert "STALE" in prefix
    assert "🪦" in prefix
    assert "stale critical" in marker
    assert "unowned for" in marker
    assert "1d" in marker or "25h" in marker  # формат duration


def test_decay_critical_under_24h_returns_unchanged():
    from app.services.discord_service import _age_decay_severity

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fired_at = now - timedelta(hours=5)
    sev, prefix, marker = _age_decay_severity("critical", fired_at, acked_by=None, now=now)
    assert sev == "critical"
    assert prefix == ""
    assert marker == ""


def test_decay_warning_over_24h_no_decay():
    """B2 спецификация: warning уже non-red → decay не нужен."""
    from app.services.discord_service import _age_decay_severity

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fired_at = now - timedelta(hours=72)
    sev, prefix, marker = _age_decay_severity("warning", fired_at, acked_by=None, now=now)
    assert sev == "warning"
    assert prefix == ""
    assert marker == ""


def test_decay_critical_acked_no_decay():
    """Если есть acked_by — алерт уже взят в работу, decay не применяем."""
    from app.services.discord_service import _age_decay_severity

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fired_at = now - timedelta(hours=48)
    sev, prefix, marker = _age_decay_severity(
        "critical", fired_at, acked_by="apleshkov", now=now,
    )
    assert sev == "critical"
    assert prefix == ""
    assert marker == ""


def test_decay_no_fired_at_returns_severity_as_is():
    from app.services.discord_service import _age_decay_severity

    sev, prefix, marker = _age_decay_severity("critical", None)
    assert sev == "critical"
    assert prefix == marker == ""


def test_decay_handles_naive_datetime():
    """fired_at без tzinfo — считаем UTC."""
    from app.services.discord_service import _age_decay_severity

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fired_at_naive = (now - timedelta(hours=30)).replace(tzinfo=None)
    sev, prefix, _ = _age_decay_severity("critical", fired_at_naive, now=now)
    assert sev == "stale_critical"
    assert "STALE" in prefix


def test_decay_color_stale_returns_orange():
    from app.services.discord_service import _decay_color

    orange = _decay_color(0xE53935, "stale_critical")
    # Orange (mid) — не red.
    assert orange != 0xE53935
    # Проверим что это orange (RGB > red component, < red of original).
    assert 0xFB0000 < orange < 0xFF0000 or orange == 0xFB8C00


def test_decay_color_normal_severity_unchanged():
    from app.services.discord_service import _decay_color

    assert _decay_color(0xE53935, "critical") == 0xE53935
    assert _decay_color(0xFDD835, "warning") == 0xFDD835


# ---------------------------------------------------------------------------
# B4: similar past field rendering
# ---------------------------------------------------------------------------


def test_similar_past_field_with_deploy():
    from app.services.discord_service import _build_similar_past_field

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    similar = {
        "resolved_at": now - timedelta(days=21),
        "duration_minutes": 47,
        "resolved_by_deploy": {
            "buildtype_name": "Wo_Backend_Build",
            "build_number": "2099",
            "sha": "7eee6c1234abcd",
            "build_id": 123456,
        },
    }
    f = _build_similar_past_field(similar, now=now)
    assert f is not None
    assert "Similar past" in f["name"]
    v = f["value"]
    assert "3 weeks ago" in v
    assert "Wo_Backend_Build" in v
    assert "#2099" in v
    assert "7eee6c" in v
    assert "47m" in v


def test_similar_past_field_with_deploy_url_override():
    from app.services.discord_service import _build_similar_past_field

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    similar = {
        "resolved_at": now - timedelta(days=2),
        "duration_minutes": 15,
        "resolved_by_deploy": {
            "buildtype_name": "Wo_Statics",
            "build_number": "555",
            "sha": "abcdef",
            "url": "https://wo-teamcity.lastoasisgame.com/viewLog.html?buildId=999",
        },
    }
    f = _build_similar_past_field(similar, now=now)
    assert f is not None
    assert "viewLog.html?buildId=999" in f["value"]
    assert "2 days ago" in f["value"]


def test_similar_past_field_without_deploy():
    from app.services.discord_service import _build_similar_past_field

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    similar = {
        "resolved_at": now - timedelta(hours=5),
        "duration_minutes": 12,
        "resolved_by_deploy": None,
    }
    f = _build_similar_past_field(similar, now=now)
    assert f is not None
    assert "no deploy attribution" in f["value"]
    assert "5 hours ago" in f["value"]
    assert "12m" in f["value"]


def test_similar_past_field_none_when_empty():
    from app.services.discord_service import _build_similar_past_field

    assert _build_similar_past_field(None) is None
    assert _build_similar_past_field({}) is None


def test_similar_past_field_missing_required():
    from app.services.discord_service import _build_similar_past_field

    # Нет resolved_at
    assert _build_similar_past_field({"duration_minutes": 5}) is None
    # Нет duration
    assert _build_similar_past_field(
        {"resolved_at": datetime.now(timezone.utc)},
    ) is None


def test_similar_past_field_isoformat_resolved_at():
    """resolved_at может прийти строкой (из Redis-кэша)."""
    from app.services.discord_service import _build_similar_past_field

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(days=10)).isoformat()
    similar = {
        "resolved_at": ts,
        "duration_minutes": 33,
        "resolved_by_deploy": None,
    }
    f = _build_similar_past_field(similar, now=now)
    assert f is not None
    assert "1 week ago" in f["value"]


def test_humanize_ago_thresholds():
    from app.services.discord_service import _humanize_ago

    assert _humanize_ago(30) == "just now"
    assert _humanize_ago(60) == "1 minute ago"
    assert _humanize_ago(120) == "2 minutes ago"
    assert _humanize_ago(3600) == "1 hour ago"
    assert _humanize_ago(7200) == "2 hours ago"
    assert _humanize_ago(86400) == "1 day ago"
    assert _humanize_ago(86400 * 8) == "1 week ago"
    assert _humanize_ago(86400 * 21) == "3 weeks ago"
    assert _humanize_ago(86400 * 60) == "2 months ago"


def test_humanize_duration_seconds():
    from app.services.discord_service import _humanize_duration_seconds

    assert _humanize_duration_seconds(60) == "1m"
    assert _humanize_duration_seconds(3600) == "1h 0m"
    assert _humanize_duration_seconds(3700) == "1h 1m"
    assert _humanize_duration_seconds(90000) == "1d 1h"


# ---------------------------------------------------------------------------
# B4: end-to-end lookup (с моком SessionLocal)
# ---------------------------------------------------------------------------


def test_lookup_similar_past_returns_none_when_no_service():
    """Без alertname/service/ns → None без обращения к БД."""
    from app.services.discord_service import _lookup_similar_past_incident

    assert _lookup_similar_past_incident(None, "s", "ns") is None
    assert _lookup_similar_past_incident("A", None, "ns") is None
    assert _lookup_similar_past_incident("A", "s", None) is None


def test_lookup_similar_past_resolved_with_deploy_marker():
    """Хит в БД с raw.resolved_by_deploy → структура содержит deploy_marker."""
    from app.services.discord import embed_builder as eb

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fake_svc = MagicMock(id=42)

    fake_alert = MagicMock()
    fake_alert.resolved_at = (now - timedelta(days=3)).replace(tzinfo=None)
    fake_alert.fired_at = (now - timedelta(days=3, minutes=47)).replace(tzinfo=None)
    fake_alert.raw = {
        "resolved_by_deploy": {
            "buildtype_name": "Wo_Backend",
            "build_number": "2099",
            "sha": "7eee6c1234",
            "build_id": 555,
        },
    }

    fake_db = MagicMock()
    # 1-й query: Service.filter().one_or_none()
    svc_query = MagicMock()
    svc_query.filter.return_value.one_or_none.return_value = fake_svc
    # 2-й query: AlertEvent.filter().order_by().limit().one_or_none()
    alert_chain = MagicMock()
    alert_chain.filter.return_value.order_by.return_value.limit.return_value.one_or_none.return_value = fake_alert
    fake_db.query.side_effect = [svc_query, alert_chain]
    fake_db.close = MagicMock()

    with patch.object(eb, "SessionLocal", create=True) as _mock_session_local, \
         patch("app.database.SessionLocal", return_value=fake_db):
        out = eb._lookup_similar_past_incident(
            alertname="HighErrorRate",
            service_name="payments",
            namespace="prod-shared",
            now=now,
        )
    assert out is not None
    assert out["alertname"] == "HighErrorRate"
    assert out["service_id"] == 42
    assert out["duration_minutes"] == 47
    assert out["resolved_by_deploy"]["build_number"] == "2099"


def test_lookup_similar_past_no_match_returns_none():
    """Нет alert в окне → None."""
    from app.services.discord import embed_builder as eb

    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    fake_svc = MagicMock(id=42)

    fake_db = MagicMock()
    svc_query = MagicMock()
    svc_query.filter.return_value.one_or_none.return_value = fake_svc
    alert_chain = MagicMock()
    alert_chain.filter.return_value.order_by.return_value.limit.return_value.one_or_none.return_value = None
    fake_db.query.side_effect = [svc_query, alert_chain]
    fake_db.close = MagicMock()

    with patch("app.database.SessionLocal", return_value=fake_db):
        out = eb._lookup_similar_past_incident(
            alertname="HighErrorRate",
            service_name="payments",
            namespace="prod-shared",
            now=now,
        )
    assert out is None


def test_lookup_similar_past_no_service_resolves_returns_none():
    from app.services.discord import embed_builder as eb

    fake_db = MagicMock()
    svc_query = MagicMock()
    svc_query.filter.return_value.one_or_none.return_value = None
    fake_db.query.return_value = svc_query
    fake_db.close = MagicMock()

    with patch("app.database.SessionLocal", return_value=fake_db):
        out = eb._lookup_similar_past_incident(
            alertname="A", service_name="s", namespace="ns",
        )
    assert out is None


def test_lookup_similar_past_db_exception_returns_none():
    """Любая ошибка в БД-пути → None, не валим embed-send."""
    from app.services.discord import embed_builder as eb

    fake_db = MagicMock()
    fake_db.query.side_effect = RuntimeError("connection refused")
    fake_db.close = MagicMock()

    with patch("app.database.SessionLocal", return_value=fake_db):
        out = eb._lookup_similar_past_incident(
            alertname="A", service_name="s", namespace="ns",
        )
    assert out is None


# ---------------------------------------------------------------------------
# send_incident_report end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    from app.services import discord_service as ds
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    yield
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()


@pytest.fixture
def webhook_env(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token")
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "DISCORD_TEAM_CHANNEL_MAP", None)
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "DISCORD_INCIDENT_CHANNEL_ID", None, raising=False)
    yield


def _httpx_mock(msg_id: str = "111", status: int = 200):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {"id": msg_id}
    mock_resp.text = "ok"
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.patch = AsyncMock(return_value=mock_resp)
    return mock_client


@pytest.mark.asyncio
async def test_send_incident_decay_applies_orange_and_stale_prefix(webhook_env):
    """A2: critical 25h old + unacked → orange color + 🪦 STALE в title."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    fired_at = datetime.now(timezone.utc) - timedelta(hours=25)
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=None),
         ):
        await svc.send_incident_report(
            incident_id="INC-stale",
            alertname="HighErrorRate",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="root cause",
            resolution_quality="unresolved",
            synthesis="...",
            fired_at=fired_at,
            acked_by=None,
        )

    assert mock_client.post.await_count == 1
    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    # Orange (не red и не yellow).
    assert embed["color"] == 0xFB8C00
    # Title prefix содержит STALE + 🪦.
    assert "STALE" in embed["title"]
    assert "🪦" in embed["title"]
    # Footer содержит stale critical · unowned for ...
    assert "stale critical" in embed["footer"]["text"]
    assert "unowned for" in embed["footer"]["text"]


@pytest.mark.asyncio
async def test_send_incident_fresh_critical_stays_red(webhook_env):
    """A2: critical 5h old → красный, без STALE."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    fired_at = datetime.now(timezone.utc) - timedelta(hours=5)
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=None),
         ):
        await svc.send_incident_report(
            incident_id="INC-fresh",
            alertname="HighErrorRate",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
            fired_at=fired_at,
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert embed["color"] == 0xE53935  # red
    assert "STALE" not in embed["title"]
    assert "stale critical" not in embed["footer"]["text"]


@pytest.mark.asyncio
async def test_send_incident_warning_no_decay(webhook_env):
    """A2: warning 25h old → жёлтый, без STALE (decay только для critical)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    fired_at = datetime.now(timezone.utc) - timedelta(hours=25)
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=None),
         ):
        await svc.send_incident_report(
            incident_id="INC-warn",
            alertname="MidLoad",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="warning",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
            fired_at=fired_at,
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFDD835  # yellow/warning
    assert "STALE" not in embed["title"]


@pytest.mark.asyncio
async def test_send_incident_similar_past_field_appended(webhook_env):
    """B4: lookup возвращает hit → field "🔁 Similar past" в embed."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    now = datetime.now(timezone.utc)
    similar_hit = {
        "alertname": "HighErrorRate",
        "service_name": "svc-a",
        "namespace": "prod-shared",
        "resolved_at": now - timedelta(days=21),
        "duration_minutes": 47,
        "resolved_by_deploy": {
            "buildtype_name": "Wo_Backend",
            "build_number": "2099",
            "sha": "7eee6c1234",
            "build_id": 555,
        },
        "service_id": 42,
    }
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=similar_hit),
         ):
        await svc.send_incident_report(
            incident_id="INC-sim",
            alertname="HighErrorRate",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert any("Similar past" in n for n in names)
    similar_field = next(f for f in embed["fields"] if "Similar past" in f["name"])
    assert "Wo_Backend" in similar_field["value"]
    assert "#2099" in similar_field["value"]


@pytest.mark.asyncio
async def test_send_incident_no_similar_past_no_field(webhook_env):
    """B4: lookup возвращает None → field отсутствует."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=None),
         ):
        await svc.send_incident_report(
            incident_id="INC-nohit",
            alertname="UnknownAlert",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert not any("Similar past" in n for n in names)


@pytest.mark.asyncio
async def test_send_incident_similar_past_hit_without_deploy(webhook_env):
    """B4: similar hit без deploy-marker → field с 'no deploy attribution'."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    now = datetime.now(timezone.utc)
    similar_hit = {
        "alertname": "X",
        "service_name": "svc-a",
        "namespace": "prod-shared",
        "resolved_at": now - timedelta(hours=5),
        "duration_minutes": 12,
        "resolved_by_deploy": None,
        "service_id": 1,
    }
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=similar_hit),
         ):
        await svc.send_incident_report(
            incident_id="INC-no-deploy",
            alertname="X",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    similar_field = next(
        (f for f in embed["fields"] if "Similar past" in f["name"]),
        None,
    )
    assert similar_field is not None
    assert "no deploy attribution" in similar_field["value"]


@pytest.mark.asyncio
async def test_send_incident_resolved_does_not_lookup_similar(webhook_env):
    """B4: resolved incident → lookup не вызываем (поле не нужно)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock()
    lookup_mock = AsyncMock(return_value=None)
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=lookup_mock,
         ):
        await svc.send_incident_report(
            incident_id="INC-resolved",
            alertname="X",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="resolved",
            synthesis="ok",
        )
    lookup_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_incident_decay_acked_stays_red(webhook_env):
    """A2: critical 30h old, но acked_by != None → НЕ stale (взяли в работу)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    fired_at = datetime.now(timezone.utc) - timedelta(hours=30)
    mock_client = _httpx_mock()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "app.services.discord_service._lookup_similar_past_incident_cached",
             new=AsyncMock(return_value=None),
         ):
        await svc.send_incident_report(
            incident_id="INC-acked",
            alertname="HighErrorRate",
            namespace="prod-shared",
            pod="p",
            service="svc-a",
            node=None,
            severity="critical",
            cause="x",
            resolution_quality="unresolved",
            synthesis="...",
            fired_at=fired_at,
            acked_by="apleshkov",
        )

    payload = mock_client.post.await_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert embed["color"] == 0xE53935  # red, не orange
    assert "STALE" not in embed["title"]
