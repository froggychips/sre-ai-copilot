"""8 axes calculator. Discrete enums — детерминированный snapshot.

Покрытие:
- namespace tiering (dev/squad/preprod/prod/system, unknown→system)
- resource_kind mapping (sts→statefulset, pvc→pvc, unknown→unknown)
- blast_radius (StatefulSet→high, Pod→low, Deployment→medium)
- data_plane (StatefulSet→yes, Pod owner sts→yes, plain Deployment→no)
- freshness (alert_age, stale_class)
- confidence (resolved_via length, target known/unknown)
- reversibility (Deployment with previous_revision→easy, sts pod→hard)
- idempotency (delete→safe, rollout_undo→guarded, scale→unsafe)
- RiskAxes.to_dict() roundtrip
"""
from __future__ import annotations

from app.remediation.risk_axes import (BlastRadius, Confidence, DataPlane,
                                       Freshness, Idempotency, NamespaceTier,
                                       ResourceKind, Reversibility,
                                       compute_risk_axes)


def test_namespace_tier_prod() -> None:
    axes = compute_risk_axes({"namespace": "prod-shared", "kind": "Pod",
                              "name": "town-1"})
    assert axes.namespace_tier is NamespaceTier.PROD


def test_namespace_tier_squad() -> None:
    axes = compute_risk_axes({"namespace": "squad-3-shared", "kind": "Pod",
                              "name": "p"})
    assert axes.namespace_tier is NamespaceTier.SQUAD


def test_namespace_tier_dev() -> None:
    axes = compute_risk_axes({"namespace": "dev-1", "kind": "Pod", "name": "p"})
    assert axes.namespace_tier is NamespaceTier.DEV


def test_namespace_tier_preprod() -> None:
    axes = compute_risk_axes({"namespace": "preprod-kingdom1", "kind": "Pod",
                              "name": "p"})
    assert axes.namespace_tier is NamespaceTier.PREPROD


def test_namespace_tier_system_known() -> None:
    axes = compute_risk_axes({"namespace": "kube-system", "kind": "Pod",
                              "name": "p"})
    assert axes.namespace_tier is NamespaceTier.SYSTEM


def test_namespace_tier_unknown_is_system() -> None:
    """Unknown prefix → system (наиболее ограничительный)."""
    axes = compute_risk_axes({"namespace": "wat-cluster-x", "kind": "Pod",
                              "name": "p"})
    assert axes.namespace_tier is NamespaceTier.SYSTEM


def test_resource_kind_mapping() -> None:
    for raw, expected in (
        ("Pod", ResourceKind.POD),
        ("Deployment", ResourceKind.DEPLOYMENT),
        ("StatefulSet", ResourceKind.STATEFULSET),
        ("sts", ResourceKind.STATEFULSET),
        ("Job", ResourceKind.JOB),
        ("CronJob", ResourceKind.JOB),
        ("PVC", ResourceKind.PVC),
        ("Secret", ResourceKind.SECRET),
        ("XYZ", ResourceKind.UNKNOWN),
        (None, ResourceKind.UNKNOWN),
    ):
        axes = compute_risk_axes(
            {"namespace": "dev-1", "kind": raw, "name": "x"},
        )
        assert axes.resource_kind is expected, raw


def test_blast_radius_statefulset_high() -> None:
    axes = compute_risk_axes(
        {"namespace": "prod-shared", "kind": "StatefulSet", "name": "town-db"},
    )
    assert axes.blast_radius is BlastRadius.HIGH


def test_blast_radius_pod_low() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Pod", "name": "p"},
    )
    assert axes.blast_radius is BlastRadius.LOW


def test_blast_radius_deployment_medium() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc"},
    )
    assert axes.blast_radius is BlastRadius.MEDIUM


def test_blast_radius_large_replicas_high() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc",
         "replicas": 12},
    )
    assert axes.blast_radius is BlastRadius.HIGH


def test_data_plane_statefulset_yes() -> None:
    axes = compute_risk_axes(
        {"namespace": "prod-shared", "kind": "StatefulSet", "name": "town-db"},
    )
    assert axes.data_plane is DataPlane.YES


def test_data_plane_pod_with_sts_owner_yes() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Pod", "name": "p",
         "owner_kind": "StatefulSet"},
    )
    assert axes.data_plane is DataPlane.YES


def test_data_plane_plain_deployment_no() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc"},
    )
    assert axes.data_plane is DataPlane.NO


def test_data_plane_label_hint_maybe() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc",
         "labels": {"data-plane": "true"}},
    )
    assert axes.data_plane is DataPlane.MAYBE


def test_freshness_fresh_default() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Pod", "name": "p"},
    )
    assert axes.freshness is Freshness.FRESH


def test_freshness_chronic_via_stale_class() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc"},
        classification_signals={"stale_class": "chronic"},
    )
    assert axes.freshness is Freshness.CHRONIC


def test_freshness_stale_via_age() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Pod", "name": "p"},
        classification_signals={"alert_age_minutes": 60 * 48},  # 48h
    )
    assert axes.freshness is Freshness.STALE


def test_confidence_weak_when_target_unknown() -> None:
    axes = compute_risk_axes({"namespace": "dev-1"})  # no kind/name
    assert axes.confidence is Confidence.WEAK


def test_confidence_strong_when_multi_signal_resolved() -> None:
    axes = compute_risk_axes(
        {
            "namespace": "dev-1", "kind": "Deployment", "name": "svc",
            "resolved_via": ["alert_label", "kg_service"],
        },
    )
    assert axes.confidence is Confidence.STRONG


def test_confidence_medium_default() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc",
         "resolved_via": ["alert_label"]},
    )
    assert axes.confidence is Confidence.MEDIUM


def test_reversibility_statefulset_hard() -> None:
    axes = compute_risk_axes(
        {"namespace": "prod-shared", "kind": "StatefulSet", "name": "db"},
    )
    assert axes.reversibility is Reversibility.HARD


def test_reversibility_pod_owned_by_deployment_easy() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Pod", "name": "p",
         "owner_kind": "Deployment"},
    )
    assert axes.reversibility is Reversibility.EASY


def test_reversibility_job_cronjob_owner_easy() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Job", "name": "j",
         "owner_kind": "CronJob"},
    )
    assert axes.reversibility is Reversibility.EASY


def test_reversibility_job_no_owner_partial() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Job", "name": "j"},
    )
    assert axes.reversibility is Reversibility.PARTIAL


def test_idempotency_delete_safe() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Job", "name": "j"},
        playbook_hint={"command_kind": "delete"},
    )
    assert axes.idempotency is Idempotency.SAFE


def test_idempotency_rollout_undo_guarded() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc"},
        playbook_hint={"command_kind": "rollout_undo"},
    )
    assert axes.idempotency is Idempotency.GUARDED


def test_idempotency_scale_unsafe() -> None:
    axes = compute_risk_axes(
        {"namespace": "dev-1", "kind": "Deployment", "name": "svc"},
        playbook_hint={"command_kind": "scale"},
    )
    assert axes.idempotency is Idempotency.UNSAFE


def test_to_dict_roundtrip() -> None:
    axes = compute_risk_axes(
        {"namespace": "prod-shared", "kind": "StatefulSet", "name": "db"},
    )
    d = axes.to_dict()
    assert d["namespace_tier"] == "prod"
    assert d["resource_kind"] == "statefulset"
    assert d["blast_radius"] == "high"
    assert d["data_plane"] == "yes"
    # все 8 axes присутствуют
    assert set(d.keys()) == {
        "namespace_tier", "resource_kind", "blast_radius", "data_plane",
        "freshness", "confidence", "reversibility", "idempotency",
    }
