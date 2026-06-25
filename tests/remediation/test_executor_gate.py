"""Тесты детерминированного executor policy-gate (app.remediation.executor_gate).

Ключевой инвариант фикса: LLM-`risk` поле intent-а НЕ влияет на решение —
gate пересчитывает риск из структурных полей (namespace/kind/replicas).
Поэтому prompt-injection, понижающий risk до "low", не может пропустить
prod/system/data-plane-цель.

Покрытие:
- squad restart/scale → не BLOCK (разрешено)
- dev restart → не BLOCK
- prod-* (prefix) → BLOCK по namespace_tier (закрывает и H3: exact-match ns)
- system ns (monitoring) → BLOCK
- preprod-* → BLOCK (default: не в approve-allowlist)
- risk="low" в prod НЕ обходит gate (headline-инвариант)
"""
from __future__ import annotations

import pytest

from app.core.execution_dsl import ExecutionIntent
from app.remediation.executor_gate import PolicyMode, evaluate_intent_gate


def _intent(namespace: str, action: str = "restart_deployment", **kw) -> ExecutionIntent:
    data = {
        "action": action,
        "resource_type": "deployment",
        "resource_name": "town-service",
        "namespace": namespace,
        "params": kw.pop("params", {}),
        "risk": kw.pop("risk", "low"),
    }
    data.update(kw)
    return ExecutionIntent.model_validate(data)


@pytest.mark.parametrize("ns", ["squad-1", "squad-24", "dev-3"])
def test_squad_and_dev_restart_allowed(ns):
    decision = evaluate_intent_gate(_intent(ns))
    assert decision.mode != PolicyMode.BLOCK
    assert decision.mode == PolicyMode.APPROVE


def test_squad_scale_allowed():
    decision = evaluate_intent_gate(
        _intent("squad-1", action="scale_deployment", params={"replicas": 3})
    )
    assert decision.mode == PolicyMode.APPROVE


@pytest.mark.parametrize("ns", ["prod-shared", "prod-kingdom7", "production-x"])
def test_prod_prefix_blocked(ns):
    """prod-* блокируется по namespace_tier — exact-match списки этого не ловили."""
    decision = evaluate_intent_gate(_intent(ns))
    assert decision.mode == PolicyMode.BLOCK
    axes_hit = {r.get("axis") for r in decision.reasons}
    assert "namespace_tier" in axes_hit


@pytest.mark.parametrize("ns", ["monitoring", "logging", "sre-ai"])
def test_system_ns_blocked(ns):
    decision = evaluate_intent_gate(_intent(ns))
    assert decision.mode == PolicyMode.BLOCK


@pytest.mark.parametrize("ns", ["preprod-shared", "stage-1"])
def test_preprod_blocked_by_default(ns):
    """preprod не в approve-allowlist → default BLOCK (no_matching_policy)."""
    decision = evaluate_intent_gate(_intent(ns))
    assert decision.mode == PolicyMode.BLOCK


def test_llm_low_risk_cannot_bypass_prod_gate():
    """Headline: даже risk='low' (как при prompt-injection) не пропускает prod."""
    decision = evaluate_intent_gate(_intent("prod-kingdom7", risk="low"))
    assert decision.mode == PolicyMode.BLOCK
