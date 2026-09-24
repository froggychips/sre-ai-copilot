"""`truncatedAlerts` из webhook-а AM: усечённый batch не должен выглядеть полным.

У receiver-ов copilot-а стоит `max_alerts: 10`: группа шире приходит
десятью алертами и числом отброшенных. Проверяем, что это число принято
моделью, учтено в логе/метрике и проставлено на каждый инцидент batch-а.
Без живого postgres: хендлер зовём напрямую, KG-запись замокана.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.api import webhooks
from app.metrics import ALERTS_TRUNCATED
from app.models.incident import AlertManagerWebhook, Incident


def _payload(truncated=None, n_alerts=2):
    data = {
        "version": "4",
        "groupKey": "{}:{alertname=\"KubePodCrashLooping\"}",
        "status": "firing",
        "receiver": "sre-ai-preprod",
        "groupLabels": {"alertname": "KubePodCrashLooping"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "warning",
                    "namespace": "squad-1",
                    "pod": f"svc-{i}",
                },
                "annotations": {"summary": "stub"},
                "startsAt": "2026-09-24T00:00:00Z",
                "fingerprint": f"trunc-{i}",
            }
            for i in range(n_alerts)
        ],
    }
    if truncated is not None:
        data["truncatedAlerts"] = truncated
    return AlertManagerWebhook.model_validate(data)


def _counter(endpoint, receiver="sre-ai-preprod"):
    return ALERTS_TRUNCATED.labels(endpoint=endpoint, receiver=receiver)._value.get()


def test_field_defaults_to_zero_when_absent():
    """Старые AM и тестовые payload-ы поле не шлют — это «не усечено»."""
    assert _payload().truncatedAlerts == 0


def test_negative_truncated_rejected():
    with pytest.raises(ValueError):
        _payload(truncated=-1)


def test_note_truncation_zero_is_silent():
    before = _counter("unit-zero")
    assert webhooks._note_truncation(_payload(truncated=0), "unit-zero") == 0
    assert _counter("unit-zero") == before


def test_note_truncation_counts_dropped_alerts():
    before = _counter("unit")
    with patch.object(webhooks.log, "warning") as warn:
        assert webhooks._note_truncation(_payload(truncated=30), "unit") == 30
    # Метрика считает отброшенные АЛЕРТЫ, а не batch-и: 30, а не 1.
    assert _counter("unit") - before == 30
    warn.assert_called_once()
    assert warn.call_args.args[0] == "webhook.batch_truncated"
    assert warn.call_args.kwargs["delivered"] == 2
    assert warn.call_args.kwargs["truncated"] == 30


def test_from_alertmanager_carries_truncation():
    alert = _payload(truncated=5).alerts[0]
    assert Incident.from_alertmanager(alert).batch_truncated_alerts == 0
    marked = Incident.from_alertmanager(alert, batch_truncated_alerts=5)
    assert marked.batch_truncated_alerts == 5
    # Пометка попадает туда же, куда и остальной инцидент (model_dump →
    # KG / запись инцидента), а не только в лог.
    assert marked.model_dump()["batch_truncated_alerts"] == 5


def test_store_endpoint_marks_every_incident_and_reports():
    seen = []

    def _populate(db, incident):
        seen.append(incident)
        return {}

    with patch.object(webhooks.raw_collector, "ingest"), patch(
        "app.knowledge_graph.auto_populator.populate_from_incident",
        side_effect=_populate,
    ):
        result = asyncio.run(
            webhooks.alertmanager_webhook_store_only(
                _payload(truncated=7, n_alerts=3), db=MagicMock(),
            )
        )

    assert result["truncated_alerts"] == 7
    assert len(seen) == 3
    assert all(i.batch_truncated_alerts == 7 for i in seen)


def test_store_endpoint_untruncated_batch_reports_zero():
    with patch.object(webhooks.raw_collector, "ingest"), patch(
        "app.knowledge_graph.auto_populator.populate_from_incident",
        return_value={},
    ):
        result = asyncio.run(
            webhooks.alertmanager_webhook_store_only(_payload(), db=MagicMock())
        )
    assert result["truncated_alerts"] == 0
