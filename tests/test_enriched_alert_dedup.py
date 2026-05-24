"""Stage 2 — PATCH-dedup для DiscordService.send_enriched_alert.

Покрывает:
  * unit-тесты `_compute_enriched_key` (sha1-стабильность, кейсы None,
    different severity/ns → разные ключи).
  * integration через DiscordService.send_enriched_alert + mock httpx:
    - 2 firing'а одинаковых (alertname,ns,service,severity) → 1 POST + 1 PATCH;
    - Спустя >TTL → 2 POST;
    - Разные ns → 2 POST;
    - Разные severity → 2 POST (отдельные dedup-keys).

Мотивация: до Stage 2 `send_enriched_alert` POSTил на каждую (alertname,
severity)-группу AM batch'а — без content-dedup. Главный источник
тройных постов в #infra-error (preprod AM, group_interval=10m, repeat=4h
→ 18 embed/сутки на одну группу).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ───────────────────────────────────────────────────────────────────
# Unit-tests для _compute_enriched_key — pure function, без HTTP.
# ───────────────────────────────────────────────────────────────────


def test_compute_enriched_key_basic_stable():
    from app.services.discord_service import _compute_enriched_key

    k1 = _compute_enriched_key("KubePodCrashLooping", "preprod-kingdom1",
                               "auth-service", "critical")
    k2 = _compute_enriched_key("KubePodCrashLooping", "preprod-kingdom1",
                               "auth-service", "critical")
    assert k1 == k2
    assert isinstance(k1, str)
    assert len(k1) == 40  # sha1 hex


def test_compute_enriched_key_severity_split():
    """Warning и critical для одной группы → разные ключи (важно: не
    схлопывать разный severity в один embed)."""
    from app.services.discord_service import _compute_enriched_key

    k_warn = _compute_enriched_key("A", "ns", "svc", "warning")
    k_crit = _compute_enriched_key("A", "ns", "svc", "critical")
    assert k_warn != k_crit


def test_compute_enriched_key_namespace_split():
    from app.services.discord_service import _compute_enriched_key

    k1 = _compute_enriched_key("A", "preprod-kingdom1", "svc", "critical")
    k2 = _compute_enriched_key("A", "preprod-kingdom2", "svc", "critical")
    assert k1 != k2


def test_compute_enriched_key_service_split():
    from app.services.discord_service import _compute_enriched_key

    k_a = _compute_enriched_key("A", "ns", "auth-service", "critical")
    k_p = _compute_enriched_key("A", "ns", "payments-service", "critical")
    assert k_a != k_p


def test_compute_enriched_key_none_alertname_returns_none():
    from app.services.discord_service import _compute_enriched_key

    assert _compute_enriched_key("", "ns", "svc", "critical") is None
    assert _compute_enriched_key(None, "ns", "svc", "critical") is None


def test_compute_enriched_key_none_namespace_normalized():
    """None namespace не должен крашить — заменяется на маркер."""
    from app.services.discord_service import _compute_enriched_key

    k1 = _compute_enriched_key("A", None, "svc", "critical")
    k2 = _compute_enriched_key("A", "<none>", "svc", "critical")
    # Оба формы валидны и должны давать один ключ (None → <none>).
    assert k1 == k2


def test_compute_enriched_key_severity_case_insensitive():
    """severity нормализуется lower-case — Critical и critical = один embed."""
    from app.services.discord_service import _compute_enriched_key

    k1 = _compute_enriched_key("A", "ns", "svc", "Critical")
    k2 = _compute_enriched_key("A", "ns", "svc", "critical")
    assert k1 == k2


# ───────────────────────────────────────────────────────────────────
# Integration через DiscordService.send_enriched_alert.
# ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_enriched_state():
    """Чистим кэш `_recent_enriched` между тестами."""
    from app.services import discord_service as ds
    ds._recent_enriched.clear()
    yield
    ds._recent_enriched.clear()


@pytest.fixture
def webhook_env(monkeypatch):
    """Стандартный webhook-env: dry-run OFF, webhook URL, default TTL."""
    from app.config import settings

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token",
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "ENRICHED_DEDUP_WINDOW_SECONDS", 1800)
    yield


def _httpx_mock(msg_id: str = "enr-msg-1") -> AsyncMock:
    """httpx.AsyncClient mock — POST возвращает {id: msg_id}, PATCH 200."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": msg_id}
    mock_resp.text = "ok"
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.patch = AsyncMock(return_value=mock_resp)
    return mock_client


def _make_context(
    alertname: str = "KubePodCrashLooping",
    namespace: str = "preprod-kingdom1",
    service: str = "auth-service",
    severity: str = "critical",
):
    """Минимальный EnrichedContext для теста send_enriched_alert."""
    from app.models.incident import Incident
    from app.services.alert_enrichment import EnrichedContext

    inc = Incident(
        incident_id=f"FP-{alertname}-{namespace}-{time.time_ns()}",
        severity=severity,
        status="firing",
        summary=f"{alertname} in {namespace}",
        description="test",
        namespace=namespace,
        labels={"alertname": alertname, "severity": severity, "namespace": namespace},
        starts_at="2026-05-24T12:00:00Z",
    )
    return EnrichedContext(
        incident=inc,
        service=service,
        pod=f"{service}-pod-x",
        team_owner="platform",
        in_kg=True,
    )


@pytest.mark.asyncio
async def test_two_identical_firings_post_then_patch(webhook_env):
    """Два firing'а одинаковых (alertname,ns,service,severity) →
    1 POST + 1 PATCH (counter ×2)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="enr-msg-1")
    ctx = _make_context()

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_enriched_alert([ctx], env="preprod")
        await svc.send_enriched_alert([_make_context()], env="preprod")

    assert mock_client.post.await_count == 1, (
        "second firing with identical content must NOT trigger second POST"
    )
    assert mock_client.patch.await_count == 1

    # footer патча должен показать ×2
    final_patch = mock_client.patch.await_args_list[-1].kwargs["json"]
    footer = final_patch["embeds"][0]["footer"]["text"]
    assert "×2" in footer
    assert "30мин" in footer


@pytest.mark.asyncio
async def test_after_ttl_new_post(webhook_env, monkeypatch):
    """После >TTL — кэш протух, второй firing идёт как новый POST."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="enr-msg-2")

    # Сжимаем TTL до 1 секунды чтобы не sleep'ить полчаса.
    from app.config import settings
    monkeypatch.setattr(settings, "ENRICHED_DEDUP_WINDOW_SECONDS", 1)

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_enriched_alert([_make_context()], env="preprod")
        # Шагаем «вперёд во времени» — патчим time.time внутри service.
        real_time = time.time
        with patch("app.services.discord.service.time.time",
                   return_value=real_time() + 10):
            await svc.send_enriched_alert([_make_context()], env="preprod")

    assert mock_client.post.await_count == 2, (
        "после TTL expiry второй firing должен идти как POST, не PATCH"
    )
    assert mock_client.patch.await_count == 0


@pytest.mark.asyncio
async def test_different_namespace_two_posts(webhook_env):
    """Тот же alertname/service/severity, но разные ns → 2 отдельных POST."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="enr-ns")

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_enriched_alert(
            [_make_context(namespace="preprod-kingdom1")], env="preprod",
        )
        await svc.send_enriched_alert(
            [_make_context(namespace="preprod-kingdom2")], env="preprod",
        )

    assert mock_client.post.await_count == 2
    assert mock_client.patch.await_count == 0


@pytest.mark.asyncio
async def test_different_severity_two_posts(webhook_env):
    """Тот же alertname/ns/service, но разные severity → 2 POST.
    Warning и critical — это разные алерты по смыслу, не объединять."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="enr-sev")

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_enriched_alert(
            [_make_context(severity="warning")], env="preprod",
        )
        await svc.send_enriched_alert(
            [_make_context(severity="critical")], env="preprod",
        )

    assert mock_client.post.await_count == 2
    assert mock_client.patch.await_count == 0


@pytest.mark.asyncio
async def test_three_firings_collapse_to_one_embed(webhook_env):
    """Три firing'а подряд → 1 POST + 2 PATCH (counter ×3)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="enr-msg-3x")

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        for _ in range(3):
            await svc.send_enriched_alert([_make_context()], env="preprod")

    assert mock_client.post.await_count == 1
    assert mock_client.patch.await_count == 2
    final_patch = mock_client.patch.await_args_list[-1].kwargs["json"]
    footer = final_patch["embeds"][0]["footer"]["text"]
    assert "×3" in footer


@pytest.mark.asyncio
async def test_no_msg_id_no_dedup(webhook_env):
    """Если webhook не возвращает msg_id (wait=false) — кэш не пополняется,
    следующий firing → новый POST (не PATCH без endpoint)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 204  # без body
    mock_resp.json.return_value = {}
    mock_resp.text = ""
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.patch = AsyncMock(return_value=mock_resp)

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_enriched_alert([_make_context()], env="preprod")
        await svc.send_enriched_alert([_make_context()], env="preprod")

    # Оба идут как POST (нет msg_id → нет dedup-state).
    assert mock_client.post.await_count == 2
    assert mock_client.patch.await_count == 0
