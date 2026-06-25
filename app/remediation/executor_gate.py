"""Детерминированный серверный risk-gate для apply-пути executor-а.

Закрывает дыру, где между утверждённым ExecutionIntent и реальным
`kubectl write` стоял лишь LLM-сгенерированный `risk`-строкой
(`app/agents/fix.py` → `ExecutionIntent.risk`). LLM управляет этим полем,
а данные инцидента (логи пода, имена ресурсов, текст алерта, Jira-summary)
втекают в промпт FixAgent — значит prompt-injection мог пометить
деструктивное действие `risk: low` и пройти `_ELIGIBLE_RISKS` насквозь.

Этот модуль пересчитывает риск ДЕТЕРМИНИРОВАННО из самого intent-а через
уже существующий и протестированный движок `compute_risk_axes` +
`evaluate_policy` (`app/remediation/*`), который apply-путь раньше не
вызывал вообще (весь policy-пакет был мёртвым кодом относительно
executor-а). LLM-`risk` теперь — лишь advisory-подсказка для UI.

Ключевое свойство: `namespace` / `resource_kind` / `replicas` берутся из
структурного ExecutionIntent, а не из свободного LLM-текста, поэтому
модель не может «уговорить» gate пропустить prod/system/data-plane-цель.

NB: это НЕ classification-playbook из registry (тот матчится по классу
алерта в `build_decision_preview`). Это явный safety-инвариант именно
executor-пути для уже-утверждённого человеком прямого intent-а.
"""
from __future__ import annotations

from typing import Any, Dict

from app.core.execution_dsl import ActionType, ExecutionIntent
from app.remediation.playbook import Playbook
from app.remediation.policy import PolicyDecision, PolicyMode, evaluate_policy
from app.remediation.risk_axes import compute_risk_axes

__all__ = ["evaluate_intent_gate", "PolicyDecision", "PolicyMode"]


# command_kind hint для reversibility/idempotency-инференса в risk_axes.
_COMMAND_KIND: Dict[ActionType, str] = {
    ActionType.RESTART_DEPLOYMENT: "restart",
    ActionType.SCALE_DEPLOYMENT: "scale",
}


# Серверная safety-policy для human-approved direct-intent applies:
#   - block.any: prod/system ns, data-plane, необратимое, weak-confidence;
#   - approve : только dev/squad ns c data_plane no/maybe;
#   - всё остальное (preprod, unknown tier) → default BLOCK.
# `match`/`plan` присутствуют лишь чтобы удовлетворить strict-схему Playbook;
# `evaluate_policy` читает только секцию `policy`.
_EXECUTOR_GATE_POLICY: Playbook = Playbook.model_validate(
    {
        "schema_version": "remediation.playbook/v1",
        "name": "_executor_apply_gate",
        "kind": "remediation",
        "description": (
            "Deterministic safety gate for human-approved direct "
            "ExecutionIntent applies (not LLM-trusted)."
        ),
        "match": {"classification": "executor_intent"},
        "policy": {
            "approve": {
                "namespace_tier": ["dev", "squad"],
                "data_plane": ["no", "maybe"],
            },
            "block": {
                "any": {
                    "namespace_tier": ["prod", "system"],
                    "data_plane": ["yes"],
                    "reversibility": ["hard"],
                    "confidence": ["weak"],
                },
            },
        },
        "plan": {"command": ["kubectl"]},
    }
)


def _intent_to_target(intent: ExecutionIntent) -> Dict[str, Any]:
    """Спроецировать ExecutionIntent в target-dict для compute_risk_axes."""
    target: Dict[str, Any] = {
        "kind": intent.resource_type,
        "namespace": intent.namespace,
        "name": intent.resource_name,
        "labels": {},
        "resolved_via": [],
    }
    replicas = intent.params.get("replicas")
    if isinstance(replicas, int) and not isinstance(replicas, bool):
        target["replicas"] = replicas
    return target


def evaluate_intent_gate(intent: ExecutionIntent) -> PolicyDecision:
    """Детерминированно оценить ExecutionIntent против executor safety-policy.

    Возвращает `PolicyDecision`; `mode == PolicyMode.BLOCK` → реальный apply
    запрещён. Не зависит от LLM-`risk` поля intent-а — namespace/kind/replicas
    берутся из структурного intent-а.
    """
    target = _intent_to_target(intent)
    hint = {"command_kind": _COMMAND_KIND.get(intent.action, "unknown")}
    axes = compute_risk_axes(target, classification_signals=None, playbook_hint=hint)
    return evaluate_policy(_EXECUTOR_GATE_POLICY, axes, target=target)
