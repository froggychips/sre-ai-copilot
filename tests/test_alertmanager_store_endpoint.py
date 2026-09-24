"""Smoke-тест: /webhooks/alertmanager/store — KG event-store без LLM.

В отличие от полного `/webhooks/alertmanager`, store-endpoint НЕ
запускает pipeline (никаких LLM-вызовов). Только записывает в kg_alerts
через populate_from_incident.

Auth теперь fail-closed (без секрета → 401), поэтому запросы подписываем
реальным HMAC через _post_signed.
"""
import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

import pytest

from tests.conftest import requires_postgres

# Все тесты в файле зависят от module-scope `app_client` fixture, которая
# создаёт таблицы через `Base.metadata.create_all(engine)` на реальном
# postgres-engine из app.database. Без живого postgres вся группа падает
# с psycopg2.OperationalError (см. conftest для обоснования conditional skip).
pytestmark = requires_postgres

_SECRET = "store-endpoint-test-secret"


@pytest.fixture(scope="module")
def app_client():
    """TestClient без shutdown-event (то же что в test_e2e_smoke)."""
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


def test_store_endpoint_writes_kg_alert_but_not_calls_pipeline(app_client):
    """Endpoint пишет kg_alert, и pipeline (celery .delay) НЕ вызывается."""
    fingerprint = f"store-smoke-{uuid.uuid4().hex[:12]}"
    payload = {
        "version": "4",
        "groupKey": "store-smoke",
        "status": "firing",
        "receiver": "sre-copilot",
        "groupLabels": {"alertname": "KubePodCrashLooping"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                    "pod": "town-service-abc",
                },
                "annotations": {"summary": "stub", "description": "stub"},
                "startsAt": "2026-05-14T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prometheus.local",
                "fingerprint": fingerprint,
            }
        ],
    }

    with patch("app.workers.tasks.process_incident_task.delay") as mock_delay, \
         patch("app.workers.tasks.async_process_incident") as mock_async_proc:
        resp = _post_signed(app_client, "/webhooks/alertmanager/store", payload)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "stored"
        # populate_from_incident вызвался — проверяем что результат внутри.
        assert body["alerts"][0]["incident_id"] == fingerprint
        # ── CRITICAL: pipeline НЕ должен запускаться. ──────────────
        mock_delay.assert_not_called()
        mock_async_proc.assert_not_called()


def test_store_endpoint_skips_resolved_alerts(app_client):
    """Resolved alerts не пишутся в kg_alerts (только firing)."""
    fingerprint = f"store-resolved-{uuid.uuid4().hex[:12]}"
    payload = {
        "version": "4",
        "groupKey": "store-resolved",
        "status": "resolved",
        "receiver": "sre-copilot",
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": [
            {
                "status": "resolved",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                },
                "annotations": {},
                "startsAt": "2026-05-14T10:00:00Z",
                "endsAt": "2026-05-14T10:05:00Z",
                "generatorURL": "https://prometheus.local",
                "fingerprint": fingerprint,
            }
        ],
    }
    resp = _post_signed(app_client, "/webhooks/alertmanager/store", payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["alerts"][0]["result"] == "resolved-skipped"


def test_store_endpoint_handles_invalid_alert_gracefully(app_client):
    """Малформированные alerts пропускаем, batch не падает."""
    payload = {
        "version": "4",
        "groupKey": "store-invalid",
        "status": "firing",
        "receiver": "sre-copilot",
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": [
            {
                "status": "firing",
                # missing alertname — ловится validate_alert_labels
                "labels": {"namespace": "prod-kingdom1"},
                "annotations": {},
                "startsAt": "2026-05-14T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prometheus.local",
                "fingerprint": "invalid-1",
            }
        ],
    }
    resp = _post_signed(app_client, "/webhooks/alertmanager/store", payload)
    # Endpoint ловит invalid alerts на per-alert basis, не возвращает 400.
    assert resp.status_code == 202
    body = resp.json()
    # invalid alert просто пропущен — в результате его нет.
    assert body["alerts"] == []


def test_store_endpoint_rejects_unsigned_request(app_client):
    """Fail-closed на HTTP-уровне: без подписи → 401 (секрет настроен)."""
    resp = app_client.post(
        "/webhooks/alertmanager/store",
        json={
            "version": "4",
            "groupKey": "unsigned",
            "status": "firing",
            "receiver": "sre-copilot",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "https://alertmanager.local",
            "alerts": [],
        },
    )
    assert resp.status_code == 401


def test_store_endpoint_persists_batch_truncation(app_client):
    """`truncatedAlerts` доезжает до kg_alerts.raw, а не только до лога.

    enrich-and-forward (живой путь) пишет тем же populate_from_incident, так
    что проверки через /store достаточно: смотрим в саму строку БД.
    """
    from app.database import SessionLocal
    from app.knowledge_graph.schema import AlertEvent

    fingerprint = f"store-trunc-{uuid.uuid4().hex[:12]}"
    payload = {
        "version": "4",
        "groupKey": "store-trunc",
        "status": "firing",
        "receiver": "sre-copilot",
        "truncatedAlerts": 12,
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "warning",
                    "namespace": "squad-1",
                    "service": "town-service",
                },
                "annotations": {"summary": "stub", "description": "stub"},
                "startsAt": "2026-05-14T10:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }
    resp = _post_signed(app_client, "/webhooks/alertmanager/store", payload)
    assert resp.status_code == 202, resp.text
    assert resp.json()["truncated_alerts"] == 12

    db = SessionLocal()
    try:
        row = db.query(AlertEvent).filter(AlertEvent.fingerprint == fingerprint).one()
        assert (row.raw or {}).get("batch_truncated_alerts") == 12
    finally:
        db.close()


def _gen_mismatch_payload(fingerprint: str, deployment: str) -> dict:
    return {
        "version": "4",
        "groupKey": f"store-gen-{fingerprint}",
        "status": "firing",
        "receiver": "sre-copilot",
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "KubeDeploymentGenerationMismatch",
                "severity": "warning",
                "namespace": "squad-18-shared",
                "service": "vm-kube-state-metrics",
                "deployment": deployment,
            },
            "annotations": {"description": f"Deployment generation for squad-18-shared/{deployment} does not match"},
            "startsAt": "2026-09-24T09:00:00Z",
            "endsAt": None,
            "generatorURL": "https://prometheus.local",
            "fingerprint": fingerprint,
        }],
    }


@pytest.mark.parametrize("churn, expected_noise", [(True, True), (False, False)])
def test_store_marks_generation_churn_incident_as_noise(app_client, churn, expected_noise):
    """Rancher-churn GenerationMismatch в /store → инцидент noise; реальный — нет."""
    from app.database import SessionLocal
    from app.knowledge_graph.schema import AlertEvent, KGIncident

    fingerprint = f"store-gen-{uuid.uuid4().hex[:12]}"
    deployment = f"svc-{uuid.uuid4().hex[:6]}"
    target = {("squad-18-shared", deployment)} if churn else set()

    async def fake_targets(_alerts):
        return target

    with patch("app.api.webhooks._generation_churn_targets", side_effect=fake_targets):
        resp = _post_signed(app_client, "/webhooks/alertmanager/store",
                            _gen_mismatch_payload(fingerprint, deployment))
    assert resp.status_code == 202, resp.text

    db = SessionLocal()
    try:
        key = db.query(AlertEvent.incident_id).filter(AlertEvent.fingerprint == fingerprint).scalar()
        assert key, "алерт должен лечь в kg_alerts с инцидентом"
        inc = db.query(KGIncident).filter(KGIncident.incident_key == key).one()
        assert bool(inc.noise) is expected_noise
        if churn:
            assert "controller_lag_rancher_churn" in (inc.extras or {})["noise_fingerprints"][fingerprint]
    finally:
        db.close()


def test_store_churn_does_not_mark_other_alerts_of_same_deployment(app_client):
    """ReplicasMismatch того же Deployment-а в том же batch-е — не шум."""
    from app.database import SessionLocal
    from app.knowledge_graph.schema import AlertEvent, KGIncident

    deployment = f"svc-{uuid.uuid4().hex[:6]}"
    fp_gen = f"store-gen-{uuid.uuid4().hex[:12]}"
    fp_rep = f"store-rep-{uuid.uuid4().hex[:12]}"
    payload = _gen_mismatch_payload(fp_gen, deployment)
    rep = json.loads(json.dumps(payload["alerts"][0]))
    rep["labels"]["alertname"] = "KubeDeploymentReplicasMismatch"
    rep["fingerprint"] = fp_rep
    payload["alerts"].append(rep)

    async def fake_targets(_alerts):
        return {("squad-18-shared", deployment)}

    with patch("app.api.webhooks._generation_churn_targets", side_effect=fake_targets):
        resp = _post_signed(app_client, "/webhooks/alertmanager/store", payload)
    assert resp.status_code == 202, resp.text

    db = SessionLocal()
    try:
        key = db.query(AlertEvent.incident_id).filter(AlertEvent.fingerprint == fp_rep).scalar()
        inc = db.query(KGIncident).filter(KGIncident.incident_key == key).one()
        marked = (inc.extras or {}).get("noise_fingerprints") or {}
        assert fp_rep not in marked
        assert inc.noise is False, "реальный ReplicasMismatch не должен прятать инцидент"
    finally:
        db.close()


def test_store_unmarks_churn_when_deployment_starts_real_rollout(app_client):
    """Тот же fingerprint: сначала churn → noise, потом накат → noise снимается."""
    from app.database import SessionLocal
    from app.knowledge_graph.schema import AlertEvent, KGIncident

    deployment = f"svc-{uuid.uuid4().hex[:6]}"
    fingerprint = f"store-gen-{uuid.uuid4().hex[:12]}"
    targets = [{("squad-18-shared", deployment)}, set()]

    async def fake_targets(_alerts):
        return targets.pop(0)

    with patch("app.api.webhooks._generation_churn_targets", side_effect=fake_targets):
        for _ in range(2):
            p = _gen_mismatch_payload(fingerprint, deployment)
            p["groupKey"] += uuid.uuid4().hex[:4]  # другой batch — иначе anti-replay
            resp = _post_signed(app_client, "/webhooks/alertmanager/store", p)
            assert resp.status_code == 202, resp.text

    db = SessionLocal()
    try:
        key = db.query(AlertEvent.incident_id).filter(AlertEvent.fingerprint == fingerprint).scalar()
        inc = db.query(KGIncident).filter(KGIncident.incident_key == key).one()
        assert inc.noise is False
        assert fingerprint not in ((inc.extras or {}).get("noise_fingerprints") or {})
    finally:
        db.close()
