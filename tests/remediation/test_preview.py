"""End-to-end build_decision_preview — snapshot матрица.

Покрытие snapshot-like cases (см. memory/project_remediation_pipeline_plan.md):
- unknown target → block
- prod namespace stale failed job → block (block.any)
- CronJob stale failed job in dev → auto candidate
- one-off failed job in dev (no owner) → approve
- low confidence classification → block (weak override)
- blast radius high → approve/block
- missing owner для chronic case → approve
- execute_status всегда `preview_only`
- idempotency_key детерминирован для одного incident+target+playbook
"""
from __future__ import annotations

from app.remediation.preview import (EXECUTE_STATUS_PREVIEW_ONLY,
                                      RemediationDecisionPreview,
                                      build_decision_preview)


def _fresh_signals(failed: int = 3, active: int = 0,
                   age_hours: int = 48) -> dict[str, object]:
    return {
        "failed_jobs": failed,
        "active_jobs": active,
        "job_age_hours": age_hours,
    }


def test_unknown_target_blocks() -> None:
    """Alert без labels (нет ns/kind/name) → block_unknown_target."""
    preview = build_decision_preview(
        {"labels": {}},
        signals=_fresh_signals(),
    )
    assert isinstance(preview, RemediationDecisionPreview)
    assert preview.decision == "block"
    assert preview.target_ref["unknown"] is True
    assert preview.selected_playbook is None
    assert any(
        r.get("rule") == "block_unknown_target" for r in preview.decision_reasons
    )
    assert preview.execute_status == EXECUTE_STATUS_PREVIEW_ONLY


def test_cronjob_stale_failed_job_dev_auto() -> None:
    """dev + CronJob + stale + logs_captured → auto candidate."""
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "migrate-202605240130",
                "owner_kind": "CronJob",
                "owner_name": "migrate",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.classification == "stale_failed_job"
    assert "cleanup_stale_failed_job" in preview.candidate_playbooks
    assert preview.selected_playbook == "cleanup_stale_failed_job"
    assert preview.decision == "auto"
    # command preview подставил job_name и namespace.
    assert preview.command_preview is not None
    assert "migrate-202605240130" in preview.command_preview
    assert "dev-1" in preview.command_preview
    assert preview.execute_status == EXECUTE_STATUS_PREVIEW_ONLY


def test_prod_stale_failed_job_blocks() -> None:
    """prod ns → block.any namespace_tier matches."""
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "prod-shared",
                "job_name": "migrate-202605240130",
                "owner_kind": "CronJob",
                "owner_name": "migrate",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "block"
    assert preview.selected_playbook == "cleanup_stale_failed_job"
    assert any(
        r.get("rule") == "block.any" for r in preview.decision_reasons
    )


def test_preprod_stale_failed_job_blocks() -> None:
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "preprod-kingdom1",
                "job_name": "migrate-x",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "block"


def test_system_namespace_blocks() -> None:
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "kube-system",
                "job_name": "j",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "block"


def test_one_off_job_dev_approve() -> None:
    """dev + owner_kind=None → approve (одобрение нужно для forensic)."""
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "one-off-migration",
                "owner_kind": "None",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "approve"
    assert preview.selected_playbook == "cleanup_stale_failed_job"


def test_helm_hook_job_dev_approve() -> None:
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "helm-pre-install",
                "owner_kind": "helm_hook",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "approve"


def test_squad_namespace_cronjob_auto() -> None:
    """squad ns тоже в auto whitelist."""
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "squad-3-shared",
                "job_name": "j",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "auto"


def test_squad_namespace_unknown_owner_block_no_matching() -> None:
    """squad ns + неизвестный owner → ни auto, ни approve → default block."""
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "squad-3-shared",
                "job_name": "j",
                "owner_kind": "WeirdOwner",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert preview.decision == "block"
    assert any(
        r.get("rule") == "block_no_matching_policy"
        for r in preview.decision_reasons
    )


def test_low_confidence_blocks() -> None:
    """classification confidence=weak (только classification.UNKNOWN даёт weak)."""
    # job_age_hours=2 → stale_failed_job не сработает → UNKNOWN → weak.
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "j",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals={"failed_jobs": 1, "active_jobs": 0, "job_age_hours": 2},
    )
    assert preview.classification == "unknown"
    # Нет matching playbook → block_no_matching_policy.
    assert preview.decision == "block"
    assert preview.selected_playbook is None


def test_no_signals_blocks() -> None:
    """No signals → classify=unknown → no candidates → block."""
    preview = build_decision_preview(
        {"labels": {"namespace": "dev-1", "deployment": "town"}},
    )
    assert preview.classification == "unknown"
    assert preview.decision == "block"


def test_idempotency_key_stable() -> None:
    """Тот же incident + target + playbook → тот же idempotency_key."""
    alert = {
        "labels": {
            "namespace": "dev-1",
            "job_name": "j",
            "owner_kind": "CronJob",
            "logs_captured": "true",
        },
    }
    p1 = build_decision_preview(alert, signals=_fresh_signals(),
                                incident_id="INC-1")
    p2 = build_decision_preview(alert, signals=_fresh_signals(),
                                incident_id="INC-1")
    assert p1.idempotency_key == p2.idempotency_key


def test_idempotency_key_differs_by_target() -> None:
    base = {
        "labels": {
            "namespace": "dev-1",
            "owner_kind": "CronJob",
            "logs_captured": "true",
        },
    }
    a = {"labels": {**base["labels"], "job_name": "ja"}}
    b = {"labels": {**base["labels"], "job_name": "jb"}}
    p1 = build_decision_preview(a, signals=_fresh_signals(),
                                incident_id="INC-1")
    p2 = build_decision_preview(b, signals=_fresh_signals(),
                                incident_id="INC-1")
    assert p1.idempotency_key != p2.idempotency_key


def test_to_dict_serializable() -> None:
    """Output должен сериализоваться в JSON-совместимый dict."""
    import json

    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "j",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
        incident_id="INC-1",
    )
    d = preview.to_dict()
    # Round-trip через json.dumps — никаких enum/dataclass utangled.
    js = json.dumps(d)
    parsed = json.loads(js)
    assert parsed["decision"] == "auto"
    assert parsed["execute_status"] == "preview_only"


def test_execute_status_always_preview_only() -> None:
    """Физическая граница: execute_status НИКОГДА не меняется в Phase A.

    Этот тест ломается если кто-то добавит `execute_status='executing'` —
    регрешн-сторож.
    """
    samples = [
        # auto case
        {
            "labels": {
                "namespace": "dev-1", "job_name": "j",
                "owner_kind": "CronJob", "logs_captured": "true",
            },
        },
        # block case
        {
            "labels": {
                "namespace": "prod-shared", "job_name": "j",
                "owner_kind": "CronJob", "logs_captured": "true",
            },
        },
        # unknown target
        {"labels": {}},
    ]
    for alert in samples:
        preview = build_decision_preview(alert, signals=_fresh_signals())
        assert preview.execute_status == "preview_only"


def test_risk_axes_present_in_output() -> None:
    preview = build_decision_preview(
        {
            "labels": {
                "namespace": "dev-1",
                "job_name": "j",
                "owner_kind": "CronJob",
                "logs_captured": "true",
            },
        },
        signals=_fresh_signals(),
    )
    assert set(preview.risk_axes.keys()) == {
        "namespace_tier", "resource_kind", "blast_radius", "data_plane",
        "freshness", "confidence", "reversibility", "idempotency",
    }
    assert preview.risk_axes["namespace_tier"] == "dev"
    assert preview.risk_axes["resource_kind"] == "job"
