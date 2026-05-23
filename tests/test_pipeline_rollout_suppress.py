"""Unit-тесты для _filter_rollout_noise в pipeline.

Root cause #2: KubeDeployment{Generation,Replicas}Mismatch и
KubeContainerWaiting часто срабатывают как побочка rolling-update'а
(median TTR 11 мин). Если в окне ROLLOUT_SUPPRESS_WINDOW_MINUTES шёл
deploy того же сервиса → демотим severity → info → Wave 3 routing
пропускает.

Whitelist actionable: KubePodCrashLooping/KubeJobFailed/TargetDown/
KubePodNotReady — никогда не подавляются.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app.config import settings
from app.models.incident import Incident
from app.workers.pipeline import IncidentPipeline


def _make_pipeline(
    alertname: str,
    namespace: str = "preprod-kingdom2",
    deployment: str = "bot-service",
    severity: str = "warning",
) -> IncidentPipeline:
    incident_data = {
        "incident_id": "fp-rollout",
        "severity": severity,
        "status": "firing",
        "summary": "test",
        "namespace": namespace,
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "namespace": namespace,
            "deployment": deployment,
        },
        "annotations": {},
        "starts_at": "2026-05-23T10:00:00Z",
    }
    db = MagicMock()
    record = MagicMock()
    root_span = MagicMock()
    p = IncidentPipeline(incident_data, db, record, root_span)
    p.incident = Incident(**incident_data)
    return p


def _make_svc_row(svc_id: int = 1, team: str = "gameplay") -> MagicMock:
    svc = MagicMock()
    svc.id = svc_id
    svc.team_owner = team
    return svc


def _make_deploy(started_minutes_ago: int, finished: bool = True) -> MagicMock:
    now = datetime.utcnow()
    d = MagicMock()
    d.id = 42
    d.started_at = now - timedelta(minutes=started_minutes_ago)
    d.finished_at = (d.started_at + timedelta(minutes=2)) if finished else None
    return d


# ── happy path: KubeDeploymentGenerationMismatch + recent deploy → suppress ─


def test_suppress_generation_mismatch_with_recent_deploy(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubeDeploymentGenerationMismatch")

    svc = _make_svc_row()
    deploy = _make_deploy(started_minutes_ago=5)
    # query(Service).filter(...).one_or_none() → svc
    # query(Deployment).filter(...).order_by(...).first() → deploy
    p.db.query.return_value.filter.return_value.one_or_none.return_value = svc
    p.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = deploy

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)

    assert p.incident.severity == "info"
    assert p.incident.labels.get("suppress_reason") == "active_rollout"
    assert p.incident.labels.get("original_severity") == "warning"
    assert diag_ctx.get("rollout_suppressed") is True


def test_suppress_replicas_mismatch_with_recent_deploy(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubeDeploymentReplicasMismatch")
    p.db.query.return_value.filter.return_value.one_or_none.return_value = _make_svc_row()
    p.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        _make_deploy(started_minutes_ago=3)
    )

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "info"


def test_suppress_container_waiting_with_recent_deploy(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubeContainerWaiting")
    p.db.query.return_value.filter.return_value.one_or_none.return_value = _make_svc_row()
    p.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        _make_deploy(started_minutes_ago=2)
    )

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "info"


# ── negative: no recent deploy → no suppress ────────────────────────────────


def test_no_suppress_when_no_recent_deploys(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubeDeploymentGenerationMismatch")
    p.db.query.return_value.filter.return_value.one_or_none.return_value = _make_svc_row()
    p.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "warning"
    assert "suppress_reason" not in p.incident.labels


# ── negative: actionable alert (CrashLooping) → NEVER suppress ──────────────


def test_no_suppress_crashlooping_even_with_recent_deploy(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubePodCrashLooping", severity="critical")
    # БД даже не должна быть запрошена — alertname отсекается до query.
    p.db.query.side_effect = AssertionError("should not query DB for actionable alert")

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "critical"


def test_no_suppress_jobfailed_actionable(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    p = _make_pipeline("KubeJobFailed", severity="warning")
    p.db.query.side_effect = AssertionError("should not query DB for actionable alert")

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "warning"


def test_no_suppress_target_down(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    p = _make_pipeline("TargetDown", severity="critical")
    p.db.query.side_effect = AssertionError("should not query DB for actionable alert")

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "critical"


# ── feature flag disabled → never suppress ─────────────────────────────────


def test_flag_disabled_skips_entirely(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", False)
    p = _make_pipeline("KubeDeploymentGenerationMismatch")
    # DB не запрашивается, флаг проверяется первым.
    p.db.query.side_effect = AssertionError("should not query DB when disabled")

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "warning"


# ── irrelevant alertname → не трогаем ──────────────────────────────────────


def test_no_suppress_unknown_alertname(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    p = _make_pipeline("SomeRandomAlert", severity="warning")
    p.db.query.side_effect = AssertionError("should not query for unknown alertname")

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "warning"


# ── service not found in KG → silent skip ──────────────────────────────────


def test_no_suppress_when_service_not_in_kg(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    p = _make_pipeline("KubeDeploymentGenerationMismatch")
    p.db.query.return_value.filter.return_value.one_or_none.return_value = None

    diag_ctx: dict = {}
    p._filter_rollout_noise(diag_ctx)
    assert p.incident.severity == "warning"


# ── audit event emitted on suppress ────────────────────────────────────────


def test_audit_event_emitted_on_suppress(monkeypatch):
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_ENABLED", True)
    monkeypatch.setattr(settings, "ROLLOUT_SUPPRESS_WINDOW_MINUTES", 15)
    p = _make_pipeline("KubeDeploymentGenerationMismatch")
    p.db.query.return_value.filter.return_value.one_or_none.return_value = _make_svc_row()
    p.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        _make_deploy(started_minutes_ago=4)
    )

    diag_ctx: dict = {}
    with patch("app.workers.pipeline.audit_service.log_event") as mock_audit:
        p._filter_rollout_noise(diag_ctx)
    # Должен быть ровно 1 ALERT_SUPPRESSED_ROLLOUT_NOISE
    suppress_calls = [c for c in mock_audit.call_args_list
                      if c.args and c.args[0] == "ALERT_SUPPRESSED_ROLLOUT_NOISE"]
    assert len(suppress_calls) == 1
    payload = suppress_calls[0].args[1]
    assert payload["alertname"] == "KubeDeploymentGenerationMismatch"
    assert payload["service"] == "bot-service"
    assert payload["namespace"] == "preprod-kingdom2"
    assert payload["deploy_id"] == 42
    assert payload["previous_severity"] == "warning"
    assert isinstance(payload["age_seconds"], int)
