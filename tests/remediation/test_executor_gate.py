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


# ---------------------------------------------------------------------------
# Fail-closed оси (#4): data_plane / reversibility / confidence должны
# реально срабатывать — раньше labels={} и отсутствие owner_kind делали их
# вырожденными (no/partial/medium), и только namespace_tier что-то решал.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "postgres", "town-db", "statics-db-postgresql", "redis-master",
    "clickhouse", "kafka-broker", "mongo-0", "rabbitmq",
])
def test_data_plane_names_blocked_even_in_squad(name):
    """Scale/restart stateful-нагрузки блокируется по data_plane даже в squad-*."""
    decision = evaluate_intent_gate(
        _intent(
            "squad-3", action="scale_deployment",
            resource_name=name, params={"replicas": 1},
        )
    )
    assert decision.mode == PolicyMode.BLOCK
    axes_hit = {r.get("axis") for r in decision.reasons}
    assert "data_plane" in axes_hit


def test_data_plane_restart_blocked_too():
    """`kubectl rollout restart deployment/postgres -n squad-3` тоже BLOCK."""
    decision = evaluate_intent_gate(
        _intent("squad-3", resource_name="postgres")
    )
    assert decision.mode == PolicyMode.BLOCK


def test_stateless_squad_deployment_still_approved():
    """Обычный app-deployment в squad не задевается data-plane эвристикой."""
    decision = evaluate_intent_gate(
        _intent("squad-3", resource_name="town-service")
    )
    assert decision.mode == PolicyMode.APPROVE


def test_inconsistent_intent_blocked_as_weak_confidence():
    """scale_deployment с resource_type=pod — LLM перепутал поля → WEAK → BLOCK."""
    decision = evaluate_intent_gate(
        _intent(
            "squad-1", action="scale_deployment",
            resource_type="pod", params={"replicas": 2},
        )
    )
    assert decision.mode == PolicyMode.BLOCK
    axes_hit = {r.get("axis") for r in decision.reasons}
    assert "confidence" in axes_hit


def test_scale_without_replicas_blocked_as_weak_confidence():
    """scale_deployment без валидного replicas — недоинтент → WEAK → BLOCK."""
    decision = evaluate_intent_gate(
        _intent("squad-1", action="scale_deployment", params={})
    )
    assert decision.mode == PolicyMode.BLOCK
