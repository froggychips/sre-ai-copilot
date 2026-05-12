"""
Test suite for SRE AI Copilot - Unit and integration tests
"""
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pydantic import ValidationError

from app.models.incident import (
    AlertManagerAlert,
    AlertManagerWebhook,
    Incident,
)
from app.services.k8s_guard import K8sOperation, K8sSecurityGuard
from app.config import Settings


class TestIncidentModels:
    """Test incident data models and validation"""

    def test_incident_from_alertmanager(self):
        """Test creation of Incident from AlertManager alert"""
        alert = AlertManagerAlert(
            status="firing",
            labels={"severity": "critical", "alertname": "PodCrashLooping"},
            annotations={"summary": "Pod is crashing"},
            startsAt="2026-05-12T12:00:00Z",
            fingerprint="abc123",
        )

        incident = Incident.from_alertmanager(alert)
        assert incident.status == "firing"
        assert incident.severity == "critical"
        assert incident.summary == "Pod is crashing"
        assert incident.incident_id == "abc123"

    def test_alertmanager_webhook_validation(self):
        """Test AlertManager webhook structure validation"""
        payload = {
            "version": "4",
            "groupKey": "test-group",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "TestAlert"},
                    "annotations": {},
                    "startsAt": "2026-05-12T12:00:00Z",
                    "fingerprint": "fp1",
                }
            ],
        }

        webhook = AlertManagerWebhook(**payload)
        assert len(webhook.alerts) == 1
        assert webhook.groupKey == "test-group"

    def test_incident_model_dump(self):
        """Test incident serialization"""
        alert = AlertManagerAlert(
            status="resolved",
            labels={"namespace": "prod"},
            annotations={},
            startsAt="2026-05-12T12:00:00Z",
            endsAt="2026-05-12T12:30:00Z",
            fingerprint="fp2",
        )

        incident = Incident.from_alertmanager(alert)
        data = incident.model_dump()

        assert "incident_id" in data
        assert data["status"] == "resolved"
        assert data["namespace"] == "prod"


class TestK8sSecurityGuard:
    """Test Kubernetes security validation"""

    def test_forbidden_namespace_blocked(self):
        """Test that forbidden namespaces are blocked"""
        op = K8sOperation(
            verb="patch",
            resource="pods",
            namespace="kube-system",
            name="coredns",
        )

        with pytest.raises(PermissionError):
            K8sSecurityGuard.validate(op)

    def test_read_only_operation_allowed(self):
        """Test read-only operations are allowed in production namespaces"""
        op = K8sOperation(
            verb="get",
            resource="pods",
            namespace="prod",
            name="app-pod",
        )

        # Should not raise
        result = K8sSecurityGuard.validate(op)
        assert result is True

    def test_write_operation_in_prod_blocked(self):
        """Test write operations in production namespace are blocked"""
        op = K8sOperation(
            verb="delete",
            resource="pods",
            namespace="prod",
            name="app-pod",
        )

        with pytest.raises(PermissionError):
            K8sSecurityGuard.validate(op)

    def test_dev_namespace_write_allowed(self):
        """Test write operations allowed in dev namespaces"""
        op = K8sOperation(
            verb="patch",
            resource="deployments",
            namespace="squad-123",
            name="test-deploy",
        )

        result = K8sSecurityGuard.validate(op)
        assert result is True

    def test_invalid_resource_blocked(self):
        """Test invalid resource types are blocked"""
        op = K8sOperation(
            verb="get",
            resource="secrets",  # Not in ALLOWED_RESOURCES
            namespace="prod",
        )

        with pytest.raises(PermissionError):
            K8sSecurityGuard.validate(op)

    def test_namespace_pattern_validation(self):
        """Test namespace pattern matching"""
        valid_ops = [
            K8sOperation(
                verb="get", resource="pods", namespace="squad-1"
            ),
            K8sOperation(
                verb="get", resource="pods", namespace="squad-gd"
            ),
        ]

        for op in valid_ops:
            K8sSecurityGuard.validate(op)  # Should not raise


class TestConfiguration:
    """Test application configuration"""

    def test_safe_mode_validation(self):
        """SAFE_MODE=false в prod должен фейлить инициализацию (strict-блокировка)."""
        with pytest.raises(ValidationError, match="SAFE_MODE"):
            Settings(
                ENV="production",
                SAFE_MODE=False,
                ANTHROPIC_API_KEY="test-key",
                DISCORD_WEBHOOK_URL="http://test",
                ALERTMANAGER_WEBHOOK_SECRET="any-secret",
            )

    def test_prod_requires_webhook_secret(self):
        """prod без ALERTMANAGER_WEBHOOK_SECRET должен фейлить (HMAC обязателен)."""
        with pytest.raises(ValidationError, match="ALERTMANAGER_WEBHOOK_SECRET"):
            Settings(
                ENV="production",
                SAFE_MODE=True,
                ANTHROPIC_API_KEY="test-key",
                DISCORD_WEBHOOK_URL="http://test",
            )


class TestIncidentPipeline:
    """Test incident processing pipeline"""

    @pytest.mark.asyncio
    async def test_incident_context_building(self):
        """Test incident context is built correctly"""
        alert = AlertManagerAlert(
            status="firing",
            labels={"severity": "high", "alertname": "HighCpuUsage"},
            annotations={"summary": "CPU usage too high"},
            startsAt="2026-05-12T12:00:00Z",
            fingerprint="cpu-high-1",
        )

        incident = Incident.from_alertmanager(alert)

        # Verify incident has all required fields
        assert incident.incident_id is not None
        assert incident.severity == "high"
        assert incident.status == "firing"

    def test_incident_deduplication_check(self):
        """Test duplicate incident detection"""
        alerts = [
            AlertManagerAlert(
                status="firing",
                labels={"alertname": "Test1"},
                annotations={},
                startsAt="2026-05-12T12:00:00Z",
                fingerprint="fp1",
            ),
            AlertManagerAlert(
                status="firing",
                labels={"alertname": "Test1"},
                annotations={},
                startsAt="2026-05-12T12:00:00Z",
                fingerprint="fp1",
            ),
        ]

        incidents = [Incident.from_alertmanager(a) for a in alerts]

        # Same fingerprint = same incident
        assert incidents[0].incident_id == incidents[1].incident_id


class TestWebhookSecurity:
    """Test webhook security features"""

    def test_hmac_verification_enabled(self):
        """Test HMAC verification can be configured"""
        from app.config import Settings

        settings = Settings(
            ANTHROPIC_API_KEY="test",
            DISCORD_WEBHOOK_URL="http://test",
            ALERTMANAGER_WEBHOOK_SECRET="my-secret",
        )

        assert settings.ALERTMANAGER_WEBHOOK_SECRET == "my-secret"

    def test_alert_label_validation(self):
        """Test alert labels are validated for security"""
        from app.api.webhooks import validate_alert_labels

        # Valid alert
        valid_alert = AlertManagerAlert(
            status="firing",
            labels={"alertname": "PodCrashLooping", "namespace": "prod"},
            annotations={},
            startsAt="2026-05-12T12:00:00Z",
            fingerprint="fp1",
        )

        # Should not raise
        validate_alert_labels(valid_alert)

        # Invalid namespace format
        invalid_alert = AlertManagerAlert(
            status="firing",
            labels={"alertname": "PodCrashLooping", "namespace": "prod@#$"},
            annotations={},
            startsAt="2026-05-12T12:00:00Z",
            fingerprint="fp2",
        )

        with pytest.raises(Exception):
            validate_alert_labels(invalid_alert)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
