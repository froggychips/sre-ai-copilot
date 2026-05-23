"""Wave 3 #1 content-based dedup tests.

Проверяет фикс: AM минтит свежий fingerprint при каждой ре-mission, поэтому
старый ключ (alertname, ns, pod) не давал dedup в проде. Новый ключ —
(alertname, namespace, service_name, reason_normalized) — стабилен и
схлопывает логически тот же инцидент в один embed с counter.

Покрываются:
  1. 3 incident-а подряд one-and-the-same (alertname/ns/service/reason) →
     1 POST + 2 PATCH (counter ×3).
  2. Тот же content-key, но разные fingerprint (разные pod-ы / incident_id) →
     dedup всё равно срабатывает — это и есть основной fix.
  3. Разные service_name в одном ns → 2 отдельных embed-а.
  4. Incident без resolved service → fallback на fingerprint-key,
     `_compute_content_key` возвращает None, не падает.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ───────────────────────────────────────────────────────────────────
# Unit-tests для _compute_content_key — чистая функция, без HTTP-мока.
# ───────────────────────────────────────────────────────────────────


def test_compute_content_key_basic():
    from app.services.discord_service import _compute_content_key

    k = _compute_content_key(
        alertname="KubePodCrashLooping",
        namespace="preprod-kingdom1",
        service_name="auth-service",
        reason="BackOff",
    )
    assert k == "KubePodCrashLooping:preprod-kingdom1:auth-service:backoff"


def test_compute_content_key_reason_fallback_to_alertname():
    """Если reason None — берём lowercase alertname."""
    from app.services.discord_service import _compute_content_key

    k = _compute_content_key(
        alertname="KubeDeploymentReplicasMismatch",
        namespace="prod-kingdom1",
        service_name="payments-service",
        reason=None,
    )
    assert k == (
        "KubeDeploymentReplicasMismatch:prod-kingdom1:payments-service:"
        "kubedeploymentreplicasmismatch"
    )


def test_compute_content_key_none_namespace_becomes_marker():
    from app.services.discord_service import _compute_content_key

    k = _compute_content_key(
        alertname="A", namespace=None, service_name="svc",
    )
    assert k == "A:<none>:svc:a"


def test_compute_content_key_missing_service_returns_none():
    """Главный fallback-триггер: нет service_name → None, caller идёт на fingerprint."""
    from app.services.discord_service import _compute_content_key

    assert _compute_content_key("A", "ns", None) is None
    assert _compute_content_key("A", "ns", "") is None


def test_compute_content_key_missing_alertname_returns_none():
    from app.services.discord_service import _compute_content_key

    assert _compute_content_key("", "ns", "svc") is None


def test_compute_content_key_synthetic_when_no_service():
    """Без service_name, но с metric_source → synthetic-id чтобы разные
    «безымянные» алерты не схлопывались."""
    from app.services.discord_service import _compute_content_key

    k = _compute_content_key(
        alertname="ProbeFailure", namespace="prod-shared",
        service_name=None, metric_source="external_probe",
    )
    assert k == "ProbeFailure:prod-shared:<synthetic:external_probe>:probefailure"


def test_compute_content_key_collision_same_service_different_pods():
    """Главный fix: тот же content-key даже при разных pod-ах
    (AM меняет fingerprint при re-mission, но мы об этом не помним)."""
    from app.services.discord_service import _compute_content_key

    k1 = _compute_content_key(
        "KubePodCrashLooping", "preprod-kingdom1", "auth-service", "BackOff",
    )
    k2 = _compute_content_key(
        "KubePodCrashLooping", "preprod-kingdom1", "auth-service", "BackOff",
    )
    assert k1 == k2


def test_compute_content_key_different_services_diverge():
    from app.services.discord_service import _compute_content_key

    k_auth = _compute_content_key(
        "KubeDeploymentReplicasMismatch", "prod-kingdom1", "auth-service",
    )
    k_pay = _compute_content_key(
        "KubeDeploymentReplicasMismatch", "prod-kingdom1", "payments-service",
    )
    assert k_auth != k_pay


# ───────────────────────────────────────────────────────────────────
# Integration через DiscordService.send_incident_report (mock httpx).
# ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Каждый тест начинает с чистого кэша."""
    from app.services import discord_service as ds
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()
    yield
    ds._recent_incidents.clear()
    ds._recent_by_alertname.clear()


@pytest.fixture
def webhook_env(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token",
    )
    monkeypatch.setattr(settings, "DISCORD_DRY_RUN", False)
    monkeypatch.setattr(settings, "DISCORD_TEAM_CHANNEL_MAP", None)
    monkeypatch.setattr(settings, "DISCORD_BOT_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "DISCORD_INCIDENT_CHANNEL_ID", None, raising=False)
    yield


def _httpx_mock(msg_id: str = "msg-1") -> AsyncMock:
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


@pytest.mark.asyncio
async def test_three_incidents_collapse_to_one_embed(webhook_env):
    """3 одинаковых incident-а → 1 POST + 2 PATCH (counter ×3)."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="msg-1")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        for i in range(3):
            await svc.send_incident_report(
                incident_id=f"INC-{i}",
                alertname="KubePodCrashLooping",
                namespace="preprod-kingdom1",
                pod=f"auth-service-pod-{i}",  # разные pod-ы — но это OK
                service="auth-service",
                node=None,
                severity="critical",
                cause="BackOff",
                resolution_quality="unresolved",
                synthesis="...",
                pod_event_reason="BackOff",
            )
    assert mock_client.post.await_count == 1
    assert mock_client.patch.await_count == 2
    # Третий PATCH (по логике — 2-й call) должен показать ×3
    final_payload = mock_client.patch.await_args_list[-1].kwargs["json"]
    footer = final_payload["embeds"][0]["footer"]["text"]
    assert "×3 в 30мин" in footer


@pytest.mark.asyncio
async def test_same_content_different_fingerprint_dedups(webhook_env):
    """Главный fix: разные incident_id (≈ разные AM-fingerprint), но
    тот же content-key (alertname/ns/service/reason) → dedup срабатывает.

    Раньше (alertname, ns, pod)-ключ + разные pod-имена от ре-mission
    AM-fingerprint = cache miss → новый POST. Теперь — PATCH.
    """
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="m-collapse")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        # 1st incident
        await svc.send_incident_report(
            incident_id="INC-A-fp-aaaa",
            alertname="KubePodCrashLooping",
            namespace="preprod-kingdom1",
            pod="auth-pod-7d4f5b-x9k2",  # старый рантайм
            service="auth-service",
            node=None,
            severity="warning",
            cause="BackOff",
            resolution_quality="unresolved",
            synthesis="…",
            pod_event_reason="BackOff",
        )
        # 2nd incident — AM перевыпустил fingerprint, pod-имя новое
        await svc.send_incident_report(
            incident_id="INC-B-fp-bbbb",  # другой incident_id
            alertname="KubePodCrashLooping",
            namespace="preprod-kingdom1",
            pod="auth-pod-7d4f5b-totally-different-pod-name",  # другой pod
            service="auth-service",
            node=None,
            severity="warning",
            cause="BackOff",
            resolution_quality="unresolved",
            synthesis="…",
            pod_event_reason="BackOff",
        )
    # ровно 1 POST, ровно 1 PATCH — основной fix
    assert mock_client.post.await_count == 1, (
        "second incident with same content must NOT trigger second POST"
    )
    assert mock_client.patch.await_count == 1


@pytest.mark.asyncio
async def test_different_service_different_embeds(webhook_env):
    """Тот же alertname/ns, но разные service → 2 отдельных POST."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="m")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        await svc.send_incident_report(
            incident_id="INC-AUTH",
            alertname="KubeDeploymentReplicasMismatch",
            namespace="prod-kingdom1",
            pod="auth-pod",
            service="auth-service",
            node=None,
            severity="critical",
            cause="replicas mismatch",
            resolution_quality="unresolved",
            synthesis="…",
        )
        await svc.send_incident_report(
            incident_id="INC-PAY",
            alertname="KubeDeploymentReplicasMismatch",
            namespace="prod-kingdom1",
            pod="payments-pod",
            service="payments-service",
            node=None,
            severity="critical",
            cause="replicas mismatch",
            resolution_quality="unresolved",
            synthesis="…",
        )
    # Оба идут как POST — разный content-key
    assert mock_client.post.await_count == 2
    assert mock_client.patch.await_count == 0


@pytest.mark.asyncio
async def test_no_service_fallback_to_fingerprint(webhook_env):
    """Incident без service → content-key None → fingerprint fallback,
    но второй такой же по (alertname,ns,pod) всё равно дедупится."""
    from app.services.discord_service import DiscordService

    svc = DiscordService()
    mock_client = _httpx_mock(msg_id="m-fb")
    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        # 1-й — никаких service / metric_source → fallback path
        await svc.send_incident_report(
            incident_id="INC-FB-1",
            alertname="NodeFilesystemAlmostOutOfSpace",
            namespace="",
            pod="",
            service=None,
            node="dev-3",
            severity="warning",
            cause="disk pressure",
            resolution_quality="unresolved",
            synthesis="…",
        )
        # 2-й тот же ns/pod/alertname — fingerprint fallback совпадёт → PATCH
        await svc.send_incident_report(
            incident_id="INC-FB-2",
            alertname="NodeFilesystemAlmostOutOfSpace",
            namespace="",
            pod="",
            service=None,
            node="dev-3",
            severity="warning",
            cause="disk pressure",
            resolution_quality="unresolved",
            synthesis="…",
        )
    # Падать не должно. POST=1, PATCH=1 (fallback fingerprint-key совпал).
    assert mock_client.post.await_count == 1
    assert mock_client.patch.await_count == 1
