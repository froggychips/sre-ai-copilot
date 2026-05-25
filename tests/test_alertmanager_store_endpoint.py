"""Smoke-тест: /webhooks/alertmanager/store — KG event-store без LLM.

В отличие от полного `/webhooks/alertmanager`, store-endpoint НЕ
запускает pipeline (никаких LLM-вызовов). Только записывает в kg_alerts
через populate_from_incident.
"""
import uuid
from unittest.mock import patch

import pytest

from tests.conftest import requires_postgres

# Все тесты в файле зависят от module-scope `app_client` fixture, которая
# создаёт таблицы через `Base.metadata.create_all(engine)` на реальном
# postgres-engine из app.database. Без живого postgres вся группа падает
# с psycopg2.OperationalError (см. conftest для обоснования conditional skip).
pytestmark = requires_postgres


@pytest.fixture(scope="module")
def app_client():
    """TestClient без shutdown-event (то же что в test_e2e_smoke)."""
    from fastapi.testclient import TestClient

    from app.database import Base, engine
    from app.main import app

    Base.metadata.create_all(engine)
    yield TestClient(app)


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
        resp = app_client.post("/webhooks/alertmanager/store", json=payload)
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
    resp = app_client.post("/webhooks/alertmanager/store", json=payload)
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
    resp = app_client.post("/webhooks/alertmanager/store", json=payload)
    # Endpoint ловит invalid alerts на per-alert basis, не возвращает 400.
    assert resp.status_code == 202
    body = resp.json()
    # invalid alert просто пропущен — в результате его нет.
    assert body["alerts"] == []
