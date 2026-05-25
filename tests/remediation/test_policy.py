"""Policy evaluator: priority chain block.any > auto > approve > default block.

Покрытие:
- block.any перебивает auto (prod ns даже с auto-clause)
- forbidden namespace (system/prod/preprod) → block
- weak confidence → block (отдельный инвариант, не из YAML)
- approve fallback когда auto не сработал
- default block_no_matching_policy когда ни один не совпал
- reasons содержат axis/expected/value/rule
"""
from __future__ import annotations

import os

import pytest

from app.remediation.playbook import load_playbook
from app.remediation.policy import (PolicyDecision, PolicyMode, evaluate_policy)
from app.remediation.risk_axes import (BlastRadius, Confidence, DataPlane,
                                       Freshness, Idempotency, NamespaceTier,
                                       ResourceKind, Reversibility, RiskAxes)


_SAMPLE_PB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "app", "remediation", "registry", "cleanup_stale_failed_job.yaml",
)


def _axes(
    *,
    ns: NamespaceTier = NamespaceTier.DEV,
    kind: ResourceKind = ResourceKind.JOB,
    blast: BlastRadius = BlastRadius.LOW,
    data: DataPlane = DataPlane.NO,
    fresh: Freshness = Freshness.FRESH,
    conf: Confidence = Confidence.STRONG,
    rev: Reversibility = Reversibility.EASY,
    idem: Idempotency = Idempotency.SAFE,
) -> RiskAxes:
    return RiskAxes(
        namespace_tier=ns,
        resource_kind=kind,
        blast_radius=blast,
        data_plane=data,
        freshness=fresh,
        confidence=conf,
        reversibility=rev,
        idempotency=idem,
    )


@pytest.fixture
def pb_cleanup() -> object:
    return load_playbook(_SAMPLE_PB)


def test_auto_match_dev_cronjob(pb_cleanup) -> None:
    """dev + CronJob owner + logs_captured → auto."""
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(pb_cleanup, _axes(ns=NamespaceTier.DEV), target)
    assert decision.mode is PolicyMode.AUTO
    # reasons должны быть structured.
    assert any(r.get("axis") == "namespace_tier" for r in decision.reasons)
    assert any(r.get("axis") == "owner_kind" for r in decision.reasons)


def test_block_when_prod_namespace(pb_cleanup) -> None:
    """prod ns → block.any matches namespace_tier=[prod,preprod,system]."""
    target = {
        "kind": "Job", "namespace": "prod-shared", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(pb_cleanup, _axes(ns=NamespaceTier.PROD), target)
    assert decision.mode is PolicyMode.BLOCK
    # reason явно ссылается на block.any.
    assert any(r.get("rule") == "block.any" for r in decision.reasons)
    assert any(r.get("axis") == "namespace_tier" for r in decision.reasons)


def test_block_when_preprod(pb_cleanup) -> None:
    target = {
        "kind": "Job", "namespace": "preprod-shared", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(
        pb_cleanup, _axes(ns=NamespaceTier.PREPROD), target,
    )
    assert decision.mode is PolicyMode.BLOCK


def test_block_when_system(pb_cleanup) -> None:
    target = {
        "kind": "Job", "namespace": "kube-system", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(
        pb_cleanup, _axes(ns=NamespaceTier.SYSTEM), target,
    )
    assert decision.mode is PolicyMode.BLOCK


def test_approve_one_off_job_in_dev(pb_cleanup) -> None:
    """dev + owner_kind=None (one-off Job) → approve по `policy.approve`."""
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "None",
        "labels": {},
    }
    decision = evaluate_policy(pb_cleanup, _axes(ns=NamespaceTier.DEV), target)
    assert decision.mode is PolicyMode.APPROVE
    assert any(r.get("rule") == "approve" for r in decision.reasons)


def test_approve_helm_hook_in_dev(pb_cleanup) -> None:
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "helm_hook",
        "labels": {},
    }
    decision = evaluate_policy(pb_cleanup, _axes(ns=NamespaceTier.DEV), target)
    assert decision.mode is PolicyMode.APPROVE


def test_block_low_confidence(pb_cleanup) -> None:
    """confidence=weak → block (ОТДЕЛЬНЫЙ инвариант, не из YAML)."""
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(
        pb_cleanup,
        _axes(ns=NamespaceTier.DEV, conf=Confidence.WEAK),
        target,
        classification_confidence="weak",
    )
    assert decision.mode is PolicyMode.BLOCK
    assert any(r.get("rule") == "block_low_confidence" for r in decision.reasons)


def test_default_block_no_matching_policy(pb_cleanup) -> None:
    """squad ns но без owner_kind=CronJob и не None/helm_hook → ни auto ни approve."""
    target = {
        "kind": "Job", "namespace": "squad-3-shared", "name": "j",
        "owner_kind": "ExoticOwner",
        "labels": {},
    }
    decision = evaluate_policy(
        pb_cleanup, _axes(ns=NamespaceTier.SQUAD), target,
    )
    assert decision.mode is PolicyMode.BLOCK
    assert any(
        r.get("rule") == "block_no_matching_policy" for r in decision.reasons
    )


def test_block_any_overrides_auto_match(pb_cleanup) -> None:
    """Даже если auto condition потенциально совпадает, block.any wins.

    Конструируем синтетический случай: target в prod ns. Auto бы провалился
    (namespace_tier!='dev'), но если бы прошёл — block.any всё равно матчит.
    Главное — block.any returns first.
    """
    target = {
        "kind": "Job", "namespace": "prod-shared", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(
        pb_cleanup, _axes(ns=NamespaceTier.PROD), target,
    )
    assert decision.mode is PolicyMode.BLOCK
    # Первый reason — block.any (а не block_no_matching_policy).
    assert decision.reasons[0].get("rule") == "block.any"


def test_reasons_structure(pb_cleanup) -> None:
    """Reasons должны содержать axis/expected/value/rule keys где это применимо."""
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision = evaluate_policy(pb_cleanup, _axes(ns=NamespaceTier.DEV), target)
    for r in decision.reasons:
        # Хотя бы rule всегда есть.
        assert "rule" in r


def test_policy_decision_to_dict(pb_cleanup) -> None:
    target = {
        "kind": "Job", "namespace": "dev-1", "name": "j",
        "owner_kind": "CronJob",
        "labels": {"logs_captured": "true"},
    }
    decision: PolicyDecision = evaluate_policy(
        pb_cleanup, _axes(ns=NamespaceTier.DEV), target,
    )
    d = decision.to_dict()
    assert d["mode"] == "auto"
    assert isinstance(d["reasons"], list)
