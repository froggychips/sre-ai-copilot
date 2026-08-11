"""Wave 3 unit-тесты для discord_service.send_incident_report.

Покрывают:
  - #1 dedup по (alertname, ns, pod): второй incident в 30-мин окне → PATCH.
  - #2 deploy correlation block: suspect → отдельное embed-field с sha-link.
  - #3 severity-routing: info → skip, warning/critical → шлём.
  - #7 sha-link helper: gitlab markdown link.
  - #8 log error rate: пустая БД → нет поля.
  - #9 burst-aggregation по alertname.
  - #10 per-team channel routing.
  - #13 recurrence window: ×N in 24h / M in 7d.

Тесты используют DISCORD_DRY_RUN=False + мок httpx чтобы видеть фактические
POST/PATCH вызовы.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers (unit)
# ---------------------------------------------------------------------------


def test_should_route_to_error_filters_info():
    from app.services.discord_service import _should_route_to_error

    assert _should_route_to_error("critical") is True
    assert _should_route_to_error("warning") is True
    assert _should_route_to_error("info") is False
    assert _should_route_to_error("") is False
    assert _should_route_to_error(None) is False
    assert _should_route_to_error("none") is False


def test_format_sha_link_with_repo_path():
    from app.services.discord_service import _format_sha_link

    out = _format_sha_link("a1b2c3d4e5f6", "wo/backend")
    assert out.startswith("[`a1b2c3d4`](")
    assert "wo-gitlab.lastoasisgame.com/wo/backend/-/commit/a1b2c3d4e5f6" in out


def test_format_sha_link_with_repo_url():
    from app.services.discord_service import _format_sha_link

    out = _format_sha_link("abc12345", "https://wo-gitlab.lastoasisgame.com/wo/repo")
    assert "https://wo-gitlab.lastoasisgame.com/wo/repo/-/commit/abc12345" in out


def test_format_sha_link_no_repo_returns_plain_short():
    from app.services.discord_service import _format_sha_link

    out = _format_sha_link("abc12345xyz")
    assert out == "`abc12345`"


def test_format_sha_link_empty_sha():
    from app.services.discord_service import _format_sha_link

    assert _format_sha_link("") == ""
    assert _format_sha_link(None) == ""


def test_format_recurrence_tag_24h_only():
    from app.services.discord_service import _format_recurrence_tag

    out = _format_recurrence_tag(True, 5, 5)
    assert "×5 in 24h" in out
    assert "in 7d" not in out  # 7d == 24h, не дублируем


def test_format_recurrence_tag_7d_extends_24h():
    from app.services.discord_service import _format_recurrence_tag

    out = _format_recurrence_tag(True, 3, 12)
    assert "×3 in 24h" in out
    assert "12 in 7d" in out


def test_format_recurrence_tag_fallback_legacy():
    from app.services.discord_service import _format_recurrence_tag

    # is_recurrence=True, но counts нулевые → старый формат
    out = _format_recurrence_tag(True, 0, 0)
    assert "RECURRENCE" in out


def test_format_recurrence_tag_empty():
    from app.services.discord_service import _format_recurrence_tag

    assert _format_recurrence_tag(False, 0, 0) == ""
    assert _format_recurrence_tag(False, 1, 1) == ""  # 24h <= 1 → не показываем


def test_webhook_edit_endpoint():
    from app.services.discord_service import _webhook_edit_endpoint

    base = "https://discord.com/api/webhooks/123456/abcdef-token"
    assert (
        _webhook_edit_endpoint(base, "999888777")
        == f"{base}/messages/999888777"
    )
    # С wait=true query — query режется
    with_q = base + "?wait=true"
    assert _webhook_edit_endpoint(with_q, "999") == f"{base}/messages/999"


def test_ensure_wait_param():
    from app.services.discord_service import _ensure_wait_param

    base = "https://discord.com/api/webhooks/1/t"
    assert _ensure_wait_param(base) == base + "?wait=true"
    assert _ensure_wait_param(base + "?wait=true") == base + "?wait=true"
    # Если есть другой query
    assert "wait=true" in _ensure_wait_param(base + "?foo=bar")


def test_pick_webhook_url_team_map(monkeypatch):
    from app.config import settings
    from app.services import discord_service as ds

    monkeypatch.setattr(
        settings, "DISCORD_TEAM_CHANNEL_MAP",
        '{"squad-1": "https://hooks/squad1", "squad-2": "https://hooks/squad2"}',
    )
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", "https://hooks/default")
    assert ds._pick_webhook_url("squad-1") == "https://hooks/squad1"
    assert ds._pick_webhook_url("squad-2") == "https://hooks/squad2"
    assert ds._pick_webhook_url("squad-99") == "https://hooks/default"
    assert ds._pick_webhook_url(None) == "https://hooks/default"


def test_pick_webhook_url_invalid_json_fallback(monkeypatch):
    from app.config import settings
    from app.services import discord_service as ds

    monkeypatch.setattr(settings, "DISCORD_TEAM_CHANNEL_MAP", "{not-json")
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", "https://hooks/default")
    assert ds._pick_webhook_url("squad-1") == "https://hooks/default"


# ---------------------------------------------------------------------------
# Deploy correlation block (#2)
# ---------------------------------------------------------------------------


def test_build_deploy_correlation_field_suspect():
    from app.services.discord_service import _build_deploy_correlation_field

    corr = {
        "verdict": "suspect",
        "deploy": {
            "buildtype_id": "Wo_Backend_Build",
            "build_number": "1234",
            "started_at": "2026-05-22T10:00:00",
            "minutes_before_incident": 7,
            "sha": "deadbeef1234567890",
            "repo": "wo/backend",
            "triggered_by": "apleshkov",
        },
        "metrics_diff": {
            "p95_latency_ms": {"before": 100, "after": 200, "delta_pct": 100.0},
            "http_5xx_rate": {"before": 0.1, "after": 0.5, "delta_pct": 400.0},
            "cpu_pct": {"before": 30, "after": 32, "delta_pct": 6.0},  # ниже порога
        },
    }
    f = _build_deploy_correlation_field(corr)
    assert f is not None
    assert "Suspect Deploy" in f["name"]
    v = f["value"]
    assert "Wo_Backend_Build" in v
    assert "#1234" in v
    assert "7min before" in v
    assert "apleshkov" in v
    assert "deadbeef" in v
    assert "wo-gitlab.lastoasisgame.com/wo/backend/-/commit/deadbeef" in v
    assert "p95 +100%" in v
    assert "5xx +400%" in v
    assert "cpu" not in v  # delta ниже порога — не показываем


def test_build_deploy_correlation_field_ok_returns_none():
    from app.services.discord_service import _build_deploy_correlation_field

    corr = {"verdict": "ok", "deploy": {"id": 1}}
    assert _build_deploy_correlation_field(corr) is None


def test_build_deploy_correlation_field_no_deploy():
    from app.services.discord_service import _build_deploy_correlation_field

    assert _build_deploy_correlation_field({"deploy": None, "reason": "no_recent_deploy"}) is None
    assert _build_deploy_correlation_field({}) is None


# ---------------------------------------------------------------------------
# Send incident report (full path)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Каждый тест начинает с чистого кэша."""
    from app.services import discord_service as ds
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    ds._recent_enriched.clear()  # incident-дедуп переехал в общий store (fallback)
    yield
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    ds._recent_enriched.clear()  # incident-дедуп переехал в общий store (fallback)


@pytest.fixture
def webhook_env(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token")
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "DISCORD_TEAM_CHANNEL_MAP", None)
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "DISCORD_INCIDENT_CHANNEL_ID", None, raising=False)
    yield


def _httpx_mock_returning_msg(msg_id: str = "111", status: int = 200):
    """Создаёт mock httpx.AsyncClient, возвращающий wait=true ответ с msg_id."""
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
async def test_send_incident_skips_info_severity(webhook_env):
    """#3: severity=info → не шлём POST."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-1",
            alertname="TestAlert",
            namespace="prod-shared",
            pod="pod-a",
            service="svc-a",
            node=None,
            severity="info",
            cause="something",
            resolution_quality="unresolved",
            synthesis="...",
        )
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_send_incident_routes_warning(webhook_env):
    """#3: warning → шлём; embed footer содержит incident id."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m1")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-2",
            alertname="A",
            namespace="prod-shared",
            pod="p",
            service="s",
            node=None,
            severity="warning",
            cause="c",
            resolution_quality="unresolved",
            synthesis="...",
        )
    assert mock_client.post.await_count == 1
    args, kwargs = mock_client.post.await_args
    sent_url = args[0]
    payload = kwargs["json"]
    assert "wait=true" in sent_url
    assert payload["embeds"][0]["footer"]["text"] == "incident/INC-2"


@pytest.mark.asyncio
async def test_send_incident_dedup_patch_on_second(webhook_env):
    """#1: повторный incident (alertname,ns,pod) <30мин → PATCH, не POST."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="msg-42")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        # 1-й — POST
        await svc.send_incident_report(
            incident_id="INC-A", alertname="X", namespace="ns", pod="pod-1",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
        assert mock_client.post.await_count == 1
        # 2-й — PATCH того же сообщения
        await svc.send_incident_report(
            incident_id="INC-B", alertname="X", namespace="ns", pod="pod-1",
            service="s", node=None, severity="critical",
            cause="c2", resolution_quality="unresolved", synthesis="...",
        )
    # POST вызвался ровно 1 раз
    assert mock_client.post.await_count == 1
    # PATCH вызвался для msg-42
    assert mock_client.patch.await_count == 1
    patch_url = mock_client.patch.await_args.args[0]
    assert "/messages/msg-42" in patch_url
    # footer обновлён с count + first/last
    patched_payload = mock_client.patch.await_args.kwargs["json"]
    footer_text = patched_payload["embeds"][0]["footer"]["text"]
    assert "×2 в 30мин" in footer_text
    assert "first" in footer_text and "last" in footer_text


@pytest.mark.asyncio
async def test_send_incident_team_routing(webhook_env, monkeypatch):
    """#10: team_owner=squad-1 → POST идёт на per-team webhook."""
    from app.config import settings
    from app.services.discord_service import DiscordService

    monkeypatch.setattr(
        settings, "DISCORD_TEAM_CHANNEL_MAP",
        '{"squad-7": "https://discord.com/api/webhooks/777/squad7-token"}',
    )
    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-T", alertname="X", namespace="ns", pod="p1",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
            team_owner="squad-7",
        )
    posted_url = mock_client.post.await_args.args[0]
    assert "/777/" in posted_url
    assert "squad7-token" in posted_url


@pytest.mark.asyncio
async def test_send_incident_deploy_correlation_field(webhook_env):
    """#2: suspect deploy → отдельное embed-поле «🔴 Suspect Deploy»."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m")
    deploy_corr = {
        "verdict": "suspect",
        "deploy": {
            "buildtype_id": "Wo_Build",
            "build_number": "100",
            "started_at": "2026-05-22T10:00:00",
            "minutes_before_incident": 3,
            "sha": "abcdef1234567890",
            "repo": "wo/repo",
            "triggered_by": "kemyashev",
        },
        "metrics_diff": {
            "http_5xx_rate": {"before": 0.1, "after": 1.0, "delta_pct": 900.0},
        },
    }
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-D", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
            deploy_correlation=deploy_corr,
        )
    payload = mock_client.post.await_args.kwargs["json"]
    fields = payload["embeds"][0]["fields"]
    suspect_fields = [f for f in fields if "Suspect Deploy" in f["name"]]
    assert len(suspect_fields) == 1
    v = suspect_fields[0]["value"]
    assert "Wo_Build" in v
    assert "kemyashev" in v
    assert "5xx +900%" in v
    assert "abcdef" in v


@pytest.mark.asyncio
async def test_send_incident_recurrence_tag_with_window(webhook_env):
    """#13: title содержит ×N in 24h / M in 7d."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-R", alertname="ChronicAlert", namespace="ns", pod="p",
            service="s", node=None, severity="warning",
            cause="c", resolution_quality="unresolved", synthesis="...",
            is_recurrence=True,
            recurrence_count_24h=8,
            recurrence_count_7d=22,
        )
    title = mock_client.post.await_args.kwargs["json"]["embeds"][0]["title"]
    assert "×8 in 24h" in title
    assert "22 in 7d" in title


@pytest.mark.asyncio
async def test_send_incident_linked_aggregation(webhook_env):
    """#9: ≥3 incident-а с одним alertname за 5 мин → последующие PATCH."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m-burst")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        # 1-й
        await svc.send_incident_report(
            incident_id="A", alertname="BurstAlert", namespace="ns1", pod="p1",
            service="s", node=None, severity="critical", cause="c",
            resolution_quality="unresolved", synthesis="...",
        )
        # 2-й — другой pod → POST новый (т.к. (alertname,ns,pod) не совпадает,
        # и порог 3 ещё не достигнут).
        await svc.send_incident_report(
            incident_id="B", alertname="BurstAlert", namespace="ns2", pod="p2",
            service="s", node=None, severity="critical", cause="c",
            resolution_quality="unresolved", synthesis="...",
        )
        # 3-й — должно дойти до linked-aggregation
        await svc.send_incident_report(
            incident_id="C", alertname="BurstAlert", namespace="ns3", pod="p3",
            service="s", node=None, severity="critical", cause="c",
            resolution_quality="unresolved", synthesis="...",
        )
    # 3 POST или 2 POST + 1 PATCH (зависит от порога). Главное — общее число
    # сообщений не растёт линейно: проверяем что хотя бы один PATCH случился
    # или последующие incident'ы аггрегированы.
    total = mock_client.post.await_count + mock_client.patch.await_count
    assert total == 3, "каждый incident должен породить ровно 1 HTTP-вызов"
    # 4-й и далее — должны PATCH'ить (linked aggregation триггерится при ≥3).
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="D", alertname="BurstAlert", namespace="ns4", pod="p4",
            service="s", node=None, severity="critical", cause="c",
            resolution_quality="unresolved", synthesis="...",
        )
    # PATCH должен сработать (4-й — linked aggregation).
    assert mock_client.patch.await_count >= 1


@pytest.mark.asyncio
async def test_send_incident_log_error_rate_field_no_db(webhook_env):
    """#8: при пустой БД log error rate field тихо отсутствует."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-L", alertname="X", namespace="ns-no-svc", pod="p",
            service="non-existent-svc", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
            incident_ts=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
    payload = mock_client.post.await_args.kwargs["json"]
    field_names = [f["name"] for f in payload["embeds"][0]["fields"]]
    # service не существует → service_id не резолвится → поле отсутствует
    assert not any("Log error rate" in n for n in field_names)


@pytest.mark.asyncio
async def test_send_incident_dry_run_no_http(webhook_env, monkeypatch):
    """DRY_RUN=True → ни POST ни PATCH не идут."""
    from app.config import settings
    from app.services.discord_service import DiscordService

    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", True)
    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        delivered = await svc.send_incident_report(
            incident_id="INC", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
    mock_client.post.assert_not_called()
    mock_client.patch.assert_not_called()
    assert delivered is True  # dry-run = намеренное подавление, не потеря


# ---------------------------------------------------------------------------
# Review-fix: 6000-char TOTAL guard + title[:256] + bool-контракт доставки.
# Раньше incident-embed уходил в Discord без _fit_embed_to_limit (он был
# только в enriched-пути) → полностью обогащённый инцидент >6000 = 400 =
# алерт дропнут целиком.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_incident_fits_embed_over_6000(webhook_env):
    """Раздутый инцидент (>6000 TOTAL) ужимается и всё равно постится."""
    from app.services.discord_service import DiscordService, _embed_total_len

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m-big")
    big = "x" * 1000
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        delivered = await svc.send_incident_report(
            incident_id="INC-BIG", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="critical",
            cause=big,  # root cause до 1024
            resolution_quality="unresolved",
            synthesis="S" * 4000,  # description → 1200 после обрезки
            deploy_correlation={
                "verdict": "likely", "confidence": 0.9,
                "deploy": {
                    "buildtype_name": "b" * 400, "number": "42",
                    "minutes_before_incident": 4,
                },
            },
            executor_result={"status": "dry_run_failed", "stderr": big},
        )
    assert mock_client.post.await_count == 1, "алерт не должен быть дропнут"
    embed = mock_client.post.await_args.kwargs["json"]["embeds"][0]
    total = _embed_total_len(embed)
    assert total <= 6000, f"embed {total} > 6000 — Discord ответит 400"
    # Root cause выживает — именно он нужен on-call.
    names = [f["name"] for f in embed["fields"]]
    assert any("Root Cause" in n for n in names)
    assert delivered is True


@pytest.mark.asyncio
async def test_send_incident_truncates_title_over_256(webhook_env):
    """Длинный alertname+ns → title режется до 256 с маркером обрезки."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m-title")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-T", alertname="A" * 300, namespace="n" * 100,
            pod="p", service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
    title = mock_client.post.await_args.kwargs["json"]["embeds"][0]["title"]
    assert len(title) <= 256
    assert title.endswith("…")


@pytest.mark.asyncio
async def test_send_incident_returns_false_on_http_error(webhook_env):
    """HTTP>=400 → False, но исключение наружу НЕ летит (контракт pipeline)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg(msg_id="m", status=400)
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        delivered = await svc.send_incident_report(
            incident_id="INC-400", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
    assert delivered is False


@pytest.mark.asyncio
async def test_send_incident_swallows_post_exception(webhook_env):
    """Исключение транспорта → False, наружу не бросаем (pipeline полагается)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg()
    mock_client.post = AsyncMock(side_effect=RuntimeError("connection reset"))
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        delivered = await svc.send_incident_report(
            incident_id="INC-EXC", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="critical",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
    assert delivered is False


@pytest.mark.asyncio
async def test_send_incident_returns_false_on_low_severity(webhook_env):
    """severity-gate skip = недоставка (False), не успех."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock_returning_msg()
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        delivered = await svc.send_incident_report(
            incident_id="INC-INFO", alertname="X", namespace="ns", pod="p",
            service="s", node=None, severity="info",
            cause="c", resolution_quality="unresolved", synthesis="...",
        )
    assert delivered is False
