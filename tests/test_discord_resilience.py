"""Resilience-тесты для DiscordService (review-fixes).

Покрывают два HIGH-фикса:
  * #3 — bounded-retry на Discord rate-limit (HTTP 429): `Retry-After`
         читается, запрос ретраится, msg_id сохраняется в dedup_store.
  * #6 — Discord 6000-char TOTAL embed limit: `_fit_embed_to_limit` дропает
         излишества enrichment-а, но title + root-cause + header остаются;
         alert не теряется целиком.

Мокаем HTTP на границе так же, как остальные discord-тесты
(patch `app.services.discord_service.httpx.AsyncClient` либо
`httpx.AsyncClient.post` — module-alias гарантирует правильный namespace).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    from app.services import discord_service as ds

    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    ds._recent_enriched.clear()
    yield
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    ds._recent_enriched.clear()


@pytest.fixture
def webhook_env(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token"
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "DISCORD_TEAM_CHANNEL_MAP", None)
    monkeypatch.setattr(settings, "DISCORD_COMPACT_MODE", "off", raising=False)
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "DISCORD_INCIDENT_CHANNEL_ID", None, raising=False)
    yield


def _mk_resp(status: int, *, msg_id: str = None, retry_after=None) -> MagicMock:
    """HTTP-response mock. retry_after → 429-тело/хедер; msg_id → wait=true 200."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = "body"
    resp.headers = {}
    if retry_after is not None:
        resp.json.return_value = {"retry_after": retry_after}
        resp.headers = {"Retry-After": str(retry_after)}
    else:
        resp.json.return_value = {"id": msg_id} if msg_id else {}
    return resp


def _mk_incident(severity: str = "critical", **overrides) -> Incident:
    labels = {
        "alertname": "KubePodCrashLooping",
        "severity": severity,
        "namespace": "prod-shared",
        "service": "svc-a",
        "pod": "svc-a-1",
    }
    kwargs = dict(
        incident_id="fp-resilience",
        severity=severity,
        status="firing",
        summary="x",
        description="desc",
        namespace="prod-shared",
        labels=labels,
        annotations={},
        starts_at="2026-07-02T00:00:00Z",
    )
    kwargs.update(overrides)
    return Incident(**kwargs)


# ---------------------------------------------------------------------------
# FIX A (#3): 429 rate-limit retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_with_ratelimit_retries_on_429_then_200():
    """429 → читаем Retry-After, спим, ретраим → 200. Возвращаем успех."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[_mk_resp(429, retry_after=0.01), _mk_resp(200, msg_id="ok")]
    )
    with patch(
        "app.services.discord_service.asyncio.sleep", new=AsyncMock()
    ) as slept:
        resp = await svc._request_with_ratelimit(
            client, "post", "https://x/wh", json={"a": 1}
        )
    assert resp.status_code == 200
    assert client.post.await_count == 2  # ретрай состоялся
    slept.assert_awaited()  # спали перед ретраем


@pytest.mark.asyncio
async def test_request_with_ratelimit_no_retry_on_non_429():
    """500/прочие 4xx НЕ ретраятся — поведение не-429 сохранено."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    client = MagicMock()
    client.post = AsyncMock(return_value=_mk_resp(500))
    with patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        resp = await svc._request_with_ratelimit(
            client, "post", "https://x/wh", json={}
        )
    assert resp.status_code == 500
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_request_with_ratelimit_bounded_attempts_on_persistent_429():
    """Постоянный 429 → капим на max_attempts (3), не крутимся вечно."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    client = MagicMock()
    client.post = AsyncMock(return_value=_mk_resp(429, retry_after=0.01))
    with patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        resp = await svc._request_with_ratelimit(
            client, "post", "https://x/wh", json={}
        )
    assert resp.status_code == 429
    assert client.post.await_count == 3  # _RATELIMIT_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_parse_retry_after_prefers_json_body_then_header():
    from app.services.discord_service import _parse_retry_after

    # JSON body (float секунды) — приоритет.
    r1 = MagicMock()
    r1.json.return_value = {"retry_after": 2.5}
    r1.headers = {"Retry-After": "9"}
    assert _parse_retry_after(r1) == 2.5

    # Нет body → header.
    r2 = MagicMock()
    r2.json.return_value = {}
    r2.headers = {"Retry-After": "4"}
    assert _parse_retry_after(r2) == 4.0

    # Ничего валидного → default.
    r3 = MagicMock()
    r3.json.side_effect = ValueError("no json")
    r3.headers = {}
    assert _parse_retry_after(r3, default=1.5) == 1.5


@pytest.mark.asyncio
async def test_send_enriched_alert_retries_429_and_saves_msg_id(
    webhook_env, monkeypatch
):
    """E2E enriched-путь: 429 → 200(wait=true) → msg_id сохранён в dedup_store.

    Раньше 429 дропался, msg_id не сохранялся → следующий идентичный alert
    ре-POSTил → снова 429 (loop). Теперь ретрай + save.
    """
    from app.services import discord_service as ds
    from app.services.discord_service import DiscordService

    ctx = EnrichedContext(incident=_mk_incident("critical"), service="svc-a", in_kg=True)

    # Свежий ключ → POST-ветка; save шпионим.
    monkeypatch.setattr(ds.dedup_store, "get_fresh", lambda *a, **k: None)
    saved: dict = {}
    monkeypatch.setattr(
        ds.dedup_store, "save", lambda key, **kw: saved.update({"key": key, **kw})
    )

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post = AsyncMock(
        side_effect=[_mk_resp(429, retry_after=0.01), _mk_resp(200, msg_id="msg-77")]
    )
    mock_client.patch = AsyncMock()

    svc = DiscordService()
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=mock_client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        await svc.send_enriched_alert([ctx], env="prod")

    assert mock_client.post.await_count == 2  # 429 → ретрай → 200
    assert saved.get("msg_id") == "msg-77"  # dedup_store пополнен после ретрая


# ---------------------------------------------------------------------------
# FIX B (#6): 6000-char TOTAL embed guard
# ---------------------------------------------------------------------------


def test_fit_embed_to_limit_trims_over_6000_keeps_root_cause():
    """Синтетический embed >6000 → после fit ≤6000, root-cause+header живы."""
    from app.services.discord_service import _embed_total_len, _fit_embed_to_limit

    big = "x" * 1000
    embed = {
        "title": "🚨 CriticalAlert · svc-a",
        "description": "d" * 1200,
        "fields": [
            {"name": "🎯 TL;DR", "value": big},
            {"name": "🎯 Скорее всего", "value": "root cause: OOMKilled ×3"},
            {"name": "Namespaces", "value": "`prod-shared`"},
            {"name": "🕒 Pod trail (Wave 7, 1h)", "value": big},
            {"name": "🌐 Endpoint health (ingress-derived)", "value": big},
            {"name": "🎯 Blast radius (Wave 7)", "value": big},
            {"name": "📨 NATS impact (Wave 7)", "value": big},
            {"name": "Upstream сейчас (KG)", "value": big},
            {"name": "🎫 Tickets (Jira, last 7d)", "value": big},
        ],
        "footer": {"text": "copilot/enrich"},
    }
    assert _embed_total_len(embed) > 6000  # заведомо превышает

    _fit_embed_to_limit(embed)

    assert _embed_total_len(embed) <= 6000
    names = [f["name"] for f in embed["fields"]]
    # Title не тронут.
    assert embed["title"] == "🚨 CriticalAlert · svc-a"
    # Root-cause + header essentials сохранены.
    assert any("Скорее всего" in n for n in names)
    assert any("TL;DR" in n for n in names)
    assert any("Namespaces" in n for n in names)
    # Низкоприоритетные излишества дропнуты первыми.
    assert not any("Pod trail" in n for n in names)
    assert not any("Blast radius" in n for n in names)


def test_fit_embed_to_limit_noop_under_limit():
    """Небольшой embed не меняется."""
    from app.services.discord_service import _fit_embed_to_limit

    embed = {
        "title": "t",
        "description": "d",
        "fields": [
            {"name": "🎯 Скорее всего", "value": "rc"},
            {"name": "🎯 Blast radius (Wave 7)", "value": "small"},
        ],
        "footer": {"text": "f"},
    }
    before = [f["name"] for f in embed["fields"]]
    _fit_embed_to_limit(embed)
    assert [f["name"] for f in embed["fields"]] == before


@pytest.mark.asyncio
async def test_send_enriched_alert_fits_over_6000_and_still_posts(
    webhook_env, monkeypatch
):
    """Полностью обогащённый critical >6000 → alert постится, embed ≤6000."""
    from app.services import discord_service as ds
    from app.services.discord_service import DiscordService, _embed_total_len

    monkeypatch.setattr(ds.dedup_store, "get_fresh", lambda *a, **k: None)
    monkeypatch.setattr(ds.dedup_store, "save", lambda *a, **k: None)

    big_name = "u" * 400
    ctx = EnrichedContext(
        incident=_mk_incident("critical", description="D" * 600),
        service="svc-a",
        pod="svc-a-1",
        in_kg=True,
        team_owner="gameplay",
        upstream_alerts=[
            {"service": big_name, "namespace": "ns", "alertname": "A", "minutes_before": 3}
            for _ in range(5)
        ],
        outgoing_deps=[
            {"kind": "calls", "service": "d" * 200, "namespace": "ns"}
            for _ in range(20)
        ],
        recent_deploys=[
            {"buildtype_name": "b" * 400, "number": str(i), "minutes_before_incident": 5}
            for i in range(3)
        ],
        jira_issues=[
            {"key": "WO-1", "summary": "j" * 80, "status": "open", "url": "http://j"}
            for _ in range(4)
        ],
        pod_events=[
            {"reason": "OOMKilled", "count": 3, "minutes_before": 2, "message": "m" * 80}
            for _ in range(5)
        ],
        blast_radius={
            "services": ["s" * 300] * 3,
            "urls": ["r" * 300] * 3,
            "services_total": 3,
            "urls_total": 3,
        },
        nats_impact=[
            {"subject": "n" * 300, "direction": "pub", "impact_count": 2, "impact_others": []}
            for _ in range(3)
        ],
        pod_trail={"total": 5, "by_reason": [("OOMKilled", 3), ("CrashLoopBackOff", 2)]},
    )

    posted: dict = {}

    async def fake_post(self, url, json=None, **_):
        posted["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    svc = DiscordService()
    with patch("httpx.AsyncClient.post", new=fake_post):
        await svc.send_enriched_alert([ctx], env="prod")

    assert "payload" in posted, "alert должен быть отправлен, а не дропнут"
    embed = posted["payload"]["embeds"][0]
    total = _embed_total_len(embed)
    assert total <= 6000, f"embed {total} > 6000 после fit"
    # Alert не потерян — title остался.
    assert embed["title"]
    # Header essential (Namespaces добавляется всегда) сохранён.
    names = [f["name"] for f in embed["fields"]]
    assert any("Namespaces" in n for n in names)
    # Излишества дропнуты (доказательство что fit сработал на >6000 embed).
    assert not any("Pod trail" in n for n in names)


# ---------------------------------------------------------------------------
# FIX C: 429-retry для остальных send-путей (review-fix).
# Раньше send_report / send_stats_report / send_external_probe_alert /
# send_self_health_alert / send_stuck_alerts_escalation шли голым client.post
# мимо _request_with_ratelimit — в alert-storm (когда они нужнее всего)
# 429 = молчаливая потеря сообщения.
# ---------------------------------------------------------------------------


def _mk_retry_client(*responses) -> AsyncMock:
    """Async-context httpx-client mock с последовательностью ответов POST."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post = AsyncMock(side_effect=list(responses))
    client.patch = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_send_report_retries_on_429(webhook_env):
    from app.services.discord_service import DiscordService

    client = _mk_retry_client(_mk_resp(429, retry_after=0.01), _mk_resp(204))
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        await DiscordService().send_report("plain report")
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_stats_report_retries_on_429_and_returns_true(monkeypatch):
    from app.config import settings
    from app.services.discord_service import DiscordService

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_STATS_URL",
        "https://discord.com/api/webhooks/2/stats-token",
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    client = _mk_retry_client(_mk_resp(429, retry_after=0.01), _mk_resp(200))
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        delivered = await DiscordService().send_stats_report("Title\nbody")
    assert delivered is True  # 429 пережит, доставка состоялась
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_external_probe_alert_retries_on_429(webhook_env):
    from app.services.discord_service import DiscordService

    client = _mk_retry_client(_mk_resp(429, retry_after=0.01), _mk_resp(204))
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        await DiscordService().send_external_probe_alert(
            host="api.example.com",
            status="down",
            snapshot={"tcp_results": [], "http_result": {}, "consecutive_failures": 3},
        )
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_self_health_alert_retries_on_429(monkeypatch):
    from app.config import settings
    from app.services.discord_service import DiscordService

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_SELF_HEALTH_URL",
        "https://discord.com/api/webhooks/3/selfhealth-token",
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    client = _mk_retry_client(_mk_resp(429, retry_after=0.01), _mk_resp(204))
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        await DiscordService().send_self_health_alert(
            failed_checks=[{"name": "sync_lag", "detail": {}}],
        )
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_stuck_alerts_escalation_retries_on_429_and_fits_embed(monkeypatch):
    """429 ретраится + embed >6000 (15 полей × ~1024) ужимается перед POST."""
    from app.config import settings
    from app.services.discord_service import DiscordService, _embed_total_len

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_STUCK_ALERTS_URL",
        "https://discord.com/api/webhooks/4/stuck-token",
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)

    # 15 команд × 10 алертов с длинными именами → каждое поле упирается
    # в свой 1024-cap → total заведомо >6000.
    team_groups = [
        {
            "team_owner": f"team-{i}",
            "alerts": [
                {
                    "alertname": "A" * 80,
                    "service": "s" * 80,
                    "hours_firing": 30.0,
                    "recurrence_24h": 5,
                }
                for _ in range(10)
            ],
        }
        for i in range(15)
    ]

    client = _mk_retry_client(_mk_resp(429, retry_after=0.01), _mk_resp(204))
    with patch(
        "app.services.discord_service.httpx.AsyncClient", return_value=client
    ), patch("app.services.discord_service.asyncio.sleep", new=AsyncMock()):
        await DiscordService().send_stuck_alerts_escalation(
            team_groups=team_groups, total_count=150, min_duration_hours=24,
        )
    assert client.post.await_count == 2
    posted = client.post.await_args.kwargs["json"]
    embed = posted["embeds"][0]
    assert _embed_total_len(embed) <= 6000, "embed обязан быть ужат перед POST"
    assert embed["title"]  # title не потерян


# ---------------------------------------------------------------------------
# FIX D: webhook-токен не утекает в discord_rate_limited лог.
# ---------------------------------------------------------------------------


def test_redact_webhook_url_strips_token():
    """url[:60] раньше захватывал первые ~8 символов токена — теперь маска."""
    from app.services.discord.service import _redact_webhook_url

    url = "https://discord.com/api/webhooks/123456789012345678/SeCrEtToKeN-abcdef?wait=true"
    redacted = _redact_webhook_url(url)
    assert "SeCrEt" not in redacted
    assert "123456789012345678" in redacted  # id вебхука остаётся для атрибуции

    # PATCH-endpoint: токен маскируется, суффикс /messages/{id} сохраняется.
    patch_url = "https://discord.com/api/webhooks/1/tok-abc/messages/42"
    redacted_patch = _redact_webhook_url(patch_url)
    assert "tok-abc" not in redacted_patch
    assert redacted_patch.endswith("/messages/42")

    # Не-webhook URL и пустая строка не ломаются.
    assert _redact_webhook_url("") == ""
    assert _redact_webhook_url("https://x/y") == "https://x/y"


@pytest.mark.asyncio
async def test_rate_limited_log_has_no_token(caplog):
    """discord_rate_limited лог на 429 не содержит webhook-токен."""
    import logging as _logging

    from app.services.discord_service import DiscordService

    svc = DiscordService()
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[_mk_resp(429, retry_after=0.01), _mk_resp(200, msg_id="ok")]
    )
    url = "https://discord.com/api/webhooks/987654321/TOPSECRETTOKEN123456"
    with patch(
        "app.services.discord_service.asyncio.sleep", new=AsyncMock()
    ), caplog.at_level(_logging.WARNING):
        await svc._request_with_ratelimit(client, "post", url, json={})
    rl_records = [r for r in caplog.records if r.getMessage() == "discord_rate_limited"]
    assert rl_records, "429 обязан логироваться"
    logged_url = rl_records[0].url
    assert "TOPSECRET" not in logged_url
    assert "987654321" in logged_url
