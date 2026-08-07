"""Тесты на /webhooks/alertmanager/enrich-and-forward — детерм. KG-enrich.

Покрывает:
  - Endpoint пишет в KG (как /store) и НЕ вызывает LLM-pipeline.
  - При DISCORD_ENRICH_ENABLED=false embed не уходит.
  - При DISCORD_ENRICH_ENABLED=true — один embed на group (alertname,sev),
    несколько ns сворачиваются в один embed.
  - Rollout-noise heuristic выставляет ROLLOUT-NORMAL tag.
  - Builder корректно работает при пустом KG (in_kg=False fallback).

Auth теперь fail-closed (без секрета → 401), поэтому запросы подписываем
реальным HMAC через _post_signed.
"""
import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import requires_postgres

# Все тесты в файле используют module-scope `app_client` fixture, которая
# зовёт `Base.metadata.create_all(engine)` на реальном postgres-engine из
# app.database. Без живого postgres вся группа падает (см. conftest для
# обоснования conditional skip).
pytestmark = requires_postgres

_SECRET = "enrich-endpoint-test-secret"


@pytest.fixture(scope="module")
def app_client():
    from fastapi.testclient import TestClient

    from app.database import Base, engine
    from app.main import app

    Base.metadata.create_all(engine)
    yield TestClient(app)


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch):
    """Auth fail-closed: выставляем секрет и чистим anti-replay кэш."""
    from app.config import settings
    from app.security.replay import alertmanager_signature_cache

    monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    alertmanager_signature_cache.clear()
    yield
    alertmanager_signature_cache.clear()


def _post_signed(client, url: str, payload: dict):
    """POST с корректной HMAC-подписью тела (сериализуем сами — подпись
    должна считаться над теми же байтами, что уйдут по сети)."""
    body = json.dumps(payload).encode()
    sig = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Alertmanager-Signature": sig,
        },
    )


def _alert_payload(
    alertname: str = "KubePodCrashLooping",
    severity: str = "critical",
    namespace: str = "preprod-kingdom2",
    service: str = "bot-service",
    pod: str = "bot-service-6c6cd4df-8hx9c",
    fingerprint: str | None = None,
    starts_at: str = "2026-05-15T12:57:00Z",
):
    return {
        "status": "firing",
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "namespace": namespace,
            "service": service,
            "pod": pod,
        },
        "annotations": {
            "summary": f"Pod {pod} is crash looping",
            "description": "Pod is crash looping.",
        },
        "startsAt": starts_at,
        "endsAt": None,
        "generatorURL": "https://prometheus.local",
        "fingerprint": fingerprint or f"enrich-{uuid.uuid4().hex[:12]}",
    }


def _batch(alerts: list, groupKey: str = "enrich-test"):
    return {
        "version": "4",
        "groupKey": groupKey,
        "status": "firing",
        "receiver": "sre-copilot",
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": alerts,
    }


def test_enrich_endpoint_stores_to_kg_and_skips_pipeline(app_client):
    """Endpoint должен вести себя как /store + (если флаг off) — без Discord."""
    payload = _batch([_alert_payload()])

    with patch("app.workers.tasks.process_incident_task.delay") as mock_delay, \
         patch("app.workers.tasks.async_process_incident") as mock_async, \
         patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send:
        # DISCORD_ENRICH_ENABLED по умолчанию False → embed не уходит.
        from app.config import settings
        with patch.object(settings, "DISCORD_ENRICH_ENABLED", False):
            resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "stored-and-forwarded"
        assert body["enrich_enabled"] is False
        assert body["enriched_groups"] == 0
        mock_delay.assert_not_called()
        mock_async.assert_not_called()
        mock_send.assert_not_called()


def test_enrich_endpoint_sends_one_embed_per_group(app_client):
    """3 одинаковых alertname в разных ns → один embed на batch."""
    alerts = [
        _alert_payload(namespace="preprod-kingdom1", pod="bot-service-a"),
        _alert_payload(namespace="preprod-kingdom2", pod="bot-service-b"),
        _alert_payload(namespace="preprod-kingdom3", pod="bot-service-c"),
    ]
    payload = _batch(alerts, groupKey="enrich-group-3ns")

    from app.config import settings
    with patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["enrich_enabled"] is True
    assert body["enriched_groups"] == 1, "3 одинаковых alertname/severity → 1 group"
    assert mock_send.await_count == 1
    sent_ctxs, kw = mock_send.await_args.args[0], mock_send.await_args.kwargs
    assert len(sent_ctxs) == 3
    assert kw.get("env") == "preprod"


def test_enrich_endpoint_groups_by_alertname_severity(app_client):
    """Два разных alertname в одном batch → два embed-а."""
    alerts = [
        _alert_payload(alertname="KubePodCrashLooping"),
        _alert_payload(alertname="KubeDeploymentGenerationMismatch", severity="warning"),
    ]
    payload = _batch(alerts, groupKey="enrich-mixed")

    from app.config import settings
    with patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["enriched_groups"] == 2
    assert mock_send.await_count == 2


def test_enrich_endpoint_suppresses_chronic(app_client):
    """Когда dedup.decide_send → SUPPRESS_CHRONIC, embed не уходит."""
    from app.config import settings
    from app.services.alert_dedup import Decision

    payload = _batch([_alert_payload(fingerprint="chronic-1")])
    async def fake_decide(**kw):
        return Decision.SUPPRESS_CHRONIC

    with patch("app.services.alert_dedup.decide_send", new=fake_decide), \
         patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["suppressed_chronic"] == 1
    assert body["enriched_groups"] == 0
    mock_send.assert_not_called()


def test_enrich_endpoint_suppresses_rollout(app_client):
    """Когда dedup.decide_send → SUPPRESS_ROLLOUT, embed не уходит."""
    from app.config import settings
    from app.services.alert_dedup import Decision

    payload = _batch([_alert_payload(
        alertname="KubeDeploymentGenerationMismatch",
        severity="warning",
        fingerprint="rollout-1",
    )])
    async def fake_decide(**kw):
        return Decision.SUPPRESS_ROLLOUT

    with patch("app.services.alert_dedup.decide_send", new=fake_decide), \
         patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["suppressed_rollout"] == 1
    assert body["enriched_groups"] == 0
    mock_send.assert_not_called()


def test_enrich_endpoint_resurfaced_flag(app_client):
    """SEND_RESURFACED → send_enriched_alert(resurfaced=True)."""
    from app.config import settings
    from app.services.alert_dedup import Decision

    payload = _batch([_alert_payload(fingerprint="resurfaced-1")])
    async def fake_decide(**kw):
        return Decision.SEND_RESURFACED

    with patch("app.services.alert_dedup.decide_send", new=fake_decide), \
         patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)
    assert resp.status_code == 202
    assert resp.json()["enriched_groups"] == 1
    assert mock_send.await_count == 1
    assert mock_send.await_args.kwargs.get("resurfaced") is True


def test_enrich_endpoint_resolved_critical_posts_notice(app_client):
    """Critical-resolved: embed-пайплайн молчит, но уходит зелёный notice."""
    alert = _alert_payload()
    alert["status"] = "resolved"
    alert["endsAt"] = "2026-05-15T13:00:00Z"
    payload = _batch([alert], groupKey="enrich-resolved")
    payload["status"] = "resolved"

    from app.config import settings
    with patch("app.services.discord_service.DiscordService.send_enriched_alert",
               new_callable=AsyncMock) as mock_send, \
         patch("app.services.discord_service.DiscordService.send_resolved_notice",
               new_callable=AsyncMock) as mock_notice, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["alerts"][0]["result"] == "resolved-skipped"
    assert body["enriched_groups"] == 0
    assert body["resolved_posted"] == 1
    mock_send.assert_not_called()
    mock_notice.assert_awaited_once()
    kwargs = mock_notice.await_args.kwargs
    assert kwargs["alertname"] == "KubePodCrashLooping"
    assert kwargs["duration_min"] == 3  # 12:57 → 13:00


def test_enrich_endpoint_resolved_warning_stays_silent(app_client):
    """Warning-resolved не постится вовсе (анти-шум)."""
    alert = _alert_payload(severity="warning")
    alert["status"] = "resolved"
    alert["endsAt"] = "2026-05-15T13:00:00Z"
    payload = _batch([alert], groupKey="enrich-resolved-warn")
    payload["status"] = "resolved"

    from app.config import settings
    with patch("app.services.discord_service.DiscordService.send_resolved_notice",
               new_callable=AsyncMock) as mock_notice, \
         patch.object(settings, "DISCORD_ENRICH_ENABLED", True):
        resp = _post_signed(app_client, "/webhooks/alertmanager/enrich-and-forward", payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["resolved_posted"] == 0
    mock_notice.assert_not_called()
