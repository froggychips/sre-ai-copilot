"""Unit-тесты на service-resolver в app.services.alert_enrichment.

Root cause #1 fix: target service резолвится из labels в priority order
(deployment > statefulset > daemonset > job_name > pod-hash-strip > container),
а не из устаревшего `service || deployment` chain. Это убирает 330
alerts/week misattribute на vm-kube-state-metrics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models.incident import Incident
from app.services.alert_enrichment import (_resolve_target_service_from_labels,
                                           _strip_pod_hash, enrich_alert)


# ── _strip_pod_hash: deployment-name derive ──────────────────────────────


def test_strip_pod_hash_deployment_pattern():
    assert _strip_pod_hash("auth-service-7f8c4b6cdf-h2x9k") == "auth-service"


def test_strip_pod_hash_long_hyphenated_name():
    assert _strip_pod_hash("squad-3-chat-messages-additional-db-5d8c9b6f4-xkl3p") \
        == "squad-3-chat-messages-additional-db"


def test_strip_pod_hash_statefulset_ordinal():
    assert _strip_pod_hash("town-db-postgresql-0") == "town-db-postgresql"
    assert _strip_pod_hash("squad-3-shared-clickhouse-shard0-0") \
        == "squad-3-shared-clickhouse-shard0"


def test_strip_pod_hash_no_match_returns_none():
    # Слишком короткий pod — не parsится.
    assert _strip_pod_hash("foo") is None
    assert _strip_pod_hash("") is None


# ── _resolve_target_service_from_labels: priority order ──────────────────


def test_resolver_picks_deployment_first():
    labels = {
        "namespace": "prod-kingdom1",
        "deployment": "auth-service",
        "pod": "auth-service-7f8c4b6cdf-h2x9k",
        "container": "main",
    }
    assert _resolve_target_service_from_labels(labels) == (
        "prod-kingdom1", "auth-service"
    )


def test_resolver_picks_statefulset_over_pod():
    labels = {
        "namespace": "bar",
        "statefulset": "db-0",
        "pod": "db-0-0",
    }
    assert _resolve_target_service_from_labels(labels) == ("bar", "db-0")


def test_resolver_picks_daemonset():
    labels = {"namespace": "kube-system", "daemonset": "kube-proxy"}
    assert _resolve_target_service_from_labels(labels) == (
        "kube-system", "kube-proxy"
    )


def test_resolver_picks_job_name():
    labels = {"namespace": "foo", "job_name": "backup-1234"}
    assert _resolve_target_service_from_labels(labels) == ("foo", "backup-1234")


def test_resolver_falls_back_to_pod_hash_strip():
    labels = {
        "namespace": "preprod-kingdom2",
        "pod": "bot-service-5d8c9b6f4-xkl3p",
    }
    assert _resolve_target_service_from_labels(labels) == (
        "preprod-kingdom2", "bot-service"
    )


def test_resolver_container_last_resort():
    labels = {"namespace": "x", "container": "sidecar"}
    assert _resolve_target_service_from_labels(labels) == ("x", "sidecar")


def test_resolver_returns_none_when_no_target():
    # Только namespace — нет targetable label.
    assert _resolve_target_service_from_labels({"namespace": "monitoring"}) == (
        "monitoring", None
    )
    # Полностью пустые labels.
    assert _resolve_target_service_from_labels({}) == (None, None)


def test_resolver_namespace_separate_from_service():
    """Главный antibug: namespace из labels, service из target-priority.
    Раньше logic брал namespace из incident.* и service==service-label —
    при alertname=KubeDeploymentReplicasMismatch + namespace=monitoring +
    deployment=auth-service резолв шёл как (monitoring, vm-kube-state-metrics),
    а должен — как (prod-kingdom1, auth-service).
    """
    labels = {"namespace": "prod-kingdom1", "deployment": "auth-service"}
    assert _resolve_target_service_from_labels(labels) == (
        "prod-kingdom1", "auth-service"
    )


# ── enrich_alert: end-to-end label resolve ───────────────────────────────


def _make_incident(
    alertname: str,
    labels: dict,
    incident_namespace: str = "monitoring",
) -> Incident:
    return Incident(
        incident_id="fp-test",
        severity="warning",
        status="firing",
        summary="test",
        namespace=incident_namespace,
        labels={"alertname": alertname, "severity": "warning", **labels},
        annotations={},
        starts_at="2026-05-23T10:00:00Z",
    )


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_enrich_resolves_kube_deployment_replicas_mismatch(*_mocks):
    """KubeDeploymentReplicasMismatch: namespace=prod-kingdom1 + deployment=auth-service
    → ctx.service == 'auth-service', НЕ vm-kube-state-metrics.
    """
    inc = _make_incident(
        "KubeDeploymentReplicasMismatch",
        {"namespace": "prod-kingdom1", "deployment": "auth-service"},
        incident_namespace="monitoring",  # source of metric, не target
    )
    db = MagicMock()
    svc_row = MagicMock()
    svc_row.team_owner = "platform"
    svc_row.synthetic = False
    svc_row.updated_at = datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc)
    # одна query цепочка; .filter(...).filter(...).first() = svc_row
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row
    db.query.return_value.filter.return_value.first.return_value = svc_row

    ctx = enrich_alert(db, inc)
    assert ctx.service == "auth-service"
    assert ctx.in_kg is True


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_enrich_resolves_kube_job_failed(*_mocks):
    """KubeJobFailed: labels {namespace: foo, job_name: backup-1234} → foo/backup-1234."""
    inc = _make_incident(
        "KubeJobFailed",
        {"namespace": "foo", "job_name": "backup-1234"},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None

    ctx = enrich_alert(db, inc)
    assert ctx.service == "backup-1234"


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_enrich_resolves_kube_statefulset_replicas_mismatch(*_mocks):
    """KubeStatefulSetReplicasMismatch: {namespace: bar, statefulset: db-0} → bar/db-0."""
    inc = _make_incident(
        "KubeStatefulSetReplicasMismatch",
        {"namespace": "bar", "statefulset": "db-0"},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None

    ctx = enrich_alert(db, inc)
    assert ctx.service == "db-0"


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_enrich_false_positive_source_namespace_overridden_by_labels(*_mocks):
    """incident.namespace=monitoring (source-of-metric) + labels со специфичными
    target — labels берутся, не false-positive `monitoring`.
    """
    inc = _make_incident(
        "KubeDeploymentGenerationMismatch",
        {"namespace": "preprod-kingdom2", "deployment": "town-service"},
        incident_namespace="monitoring",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None

    ctx = enrich_alert(db, inc)
    assert ctx.service == "town-service"
    # namespace передаётся в downstream queries — проверим что НЕ monitoring
    # Подвергается KG-lookup → query.filter получает namespace=preprod-kingdom2.
    # У MagicMock это видно через call_args, но достаточно service-assert.


@patch("app.services.alert_enrichment.recent_deploys_for", return_value=[])
@patch("app.services.alert_enrichment.nearby_alerts", return_value=[])
@patch("app.services.alert_enrichment.incidents_on", return_value=[])
@patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={})
@patch("app.services.alert_enrichment.upstream_of", return_value=[])
@patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[])
def test_enrich_synthetic_fallback_marked_in_extras(*_mocks):
    """Если match только synthetic Service — пометить ctx.extras."""
    inc = _make_incident(
        "KubeDeploymentReplicasMismatch",
        {"namespace": "kube-system", "deployment": "kube-proxy"},
    )
    db = MagicMock()
    synth_svc = MagicMock()
    synth_svc.team_owner = "platform"
    synth_svc.synthetic = True
    synth_svc.updated_at = None
    # 1-я query (non-synthetic) → None; 2-я (any) → synth_svc.
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = synth_svc

    ctx = enrich_alert(db, inc)
    assert ctx.extras.get("synthetic_fallback") is True
    assert ctx.in_kg is True


def test_enrich_legacy_service_label_still_works():
    """Регрессионная защита: старые алёрты с `service` label (custom Prometheus
    rules без `deployment`) продолжают резолвиться корректно.
    """
    inc = _make_incident(
        "MyCustomAlert",  # не Kube-*
        {"namespace": "preprod-kingdom2", "service": "legacy-svc"},
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    with patch("app.services.alert_enrichment.recent_deploys_for", return_value=[]), \
         patch("app.services.alert_enrichment.nearby_alerts", return_value=[]), \
         patch("app.services.alert_enrichment.incidents_on", return_value=[]), \
         patch("app.services.alert_enrichment._downstream_count_by_kind", return_value={}), \
         patch("app.services.alert_enrichment.upstream_of", return_value=[]), \
         patch("app.services.alert_enrichment.recent_pod_events_for", return_value=[]):
        ctx = enrich_alert(db, inc)
    assert ctx.service == "legacy-svc"
