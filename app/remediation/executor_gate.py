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

import re
from dataclasses import replace
from typing import Any, Dict

from app.core.execution_dsl import ExecutionIntent, action_spec
from app.remediation.playbook import Playbook
from app.remediation.policy import PolicyDecision, PolicyMode, evaluate_policy
from app.remediation.risk_axes import (Confidence, DataPlane, Reversibility,
                                       RiskAxes, compute_risk_axes)

__all__ = ["evaluate_intent_gate", "PolicyDecision", "PolicyMode"]


# command_kind, «мутирующее ли», требуемый resource_type и обязательные
# параметры берутся из ЕДИНОГО реестра `execution_dsl.ACTION_SPECS`.
#
# Раньше здесь лежала своя копия этого знания: словарь `_COMMAND_KIND` на два
# действия плюс условия ниже, перечислявшие те же действия ещё раз. Реестра не
# было, связи с транслятором — тоже: добавив действие в перечень и в
# `to_argv`, про guard можно было просто забыть, и новое мутирующее действие
# прошло бы проверку риска как безвредное чтение.


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


# Маркеры data-plane-нагрузок в ИМЕНИ ресурса. У прямого ExecutionIntent нет
# labels/owner-а (LLM их не выдаёт, TargetRef-резолва на apply-пути нет),
# поэтому единственный доступный сигнал data-plane — само имя. Матч по
# токенам (split по -._), startswith покрывает postgresql/mongodb/…
_DATA_PLANE_NAME_MARKERS: tuple = (
    "db", "postgres", "pg", "mysql", "mariadb", "mongo", "redis",
    "clickhouse", "kafka", "rabbitmq", "nats", "minio", "elastic",
    "etcd", "zookeeper", "cassandra", "seq",
)

_NAME_TOKEN_RE = re.compile(r"[-._]")


def _looks_data_plane(name: str) -> bool:
    """True если имя ресурса похоже на stateful/data-plane нагрузку."""
    tokens = _NAME_TOKEN_RE.split((name or "").lower())
    return any(
        t == m or t.startswith(m)
        for t in tokens if t
        for m in _DATA_PLANE_NAME_MARKERS
    )


def _intent_to_target(intent: ExecutionIntent) -> Dict[str, Any]:
    """Спроецировать ExecutionIntent в target-dict для compute_risk_axes."""
    target: Dict[str, Any] = {
        "kind": intent.resource_type,
        "namespace": intent.namespace,
        "name": intent.resource_name,
        # labels/owner_kind у intent-а недоступны — оси, которые от них
        # зависят, ужесточаются fail-closed в _apply_fail_closed_axes ниже.
        "labels": {},
        "resolved_via": [],
    }
    replicas = intent.params.get("replicas")
    if isinstance(replicas, int) and not isinstance(replicas, bool):
        target["replicas"] = replicas
    return target


def _apply_fail_closed_axes(axes: RiskAxes, intent: ExecutionIntent) -> RiskAxes:
    """Ужесточить оси, для которых у прямого intent-а нет полного сигнала.

    compute_risk_axes спроектирован под TargetRef c labels/owner_kind/
    resolved_via; у ExecutionIntent их нет, и без коррекции оси data_plane/
    reversibility/confidence вырождались в no/partial/medium — то есть
    НИКОГДА не срабатывали в block.any (`kubectl scale deployment/postgres`
    проходил как safe). Правило: неизвестный сигнал = риск, не «нет риска».
    """
    spec = action_spec(intent.action)
    data_plane = axes.data_plane
    reversibility = axes.reversibility
    confidence = axes.confidence

    # 1) data_plane: имя похоже на stateful-нагрузку → YES (block.any).
    #    Иначе labels/owner недоступны → «не data-plane» недоказуемо → MAYBE
    #    (риск учтён, но в dev/squad человек уже одобрил — approve допускает).
    if _looks_data_plane(intent.resource_name):
        data_plane = DataPlane.YES
    elif data_plane == DataPlane.NO:
        data_plane = DataPlane.MAYBE

    # 2) reversibility: рестарт/скейл data-plane-нагрузки может терять данные
    #    или кворум, а прежний replica-count нигде не записан → HARD.
    if data_plane == DataPlane.YES:
        reversibility = Reversibility.HARD

    # 3) confidence: внутренне противоречивый intent (LLM перепутал поля) —
    #    action оперирует deployment-ом, а resource_type другой; или scale
    #    без валидного replicas. Такое = слабая уверенность → WEAK (block.any).
    if spec.requires_resource_type and (
        intent.resource_type.lower() != spec.requires_resource_type
    ):
        confidence = Confidence.WEAK
    if "replicas" in spec.required_params:
        replicas = intent.params.get("replicas")
        # bool — подкласс int в Python: `True` прошло бы как «1 реплика».
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            confidence = Confidence.WEAK

    return replace(
        axes,
        data_plane=data_plane,
        reversibility=reversibility,
        confidence=confidence,
    )


def evaluate_intent_gate(intent: ExecutionIntent) -> PolicyDecision:
    """Детерминированно оценить ExecutionIntent против executor safety-policy.

    Возвращает `PolicyDecision`; `mode == PolicyMode.BLOCK` → реальный apply
    запрещён. Не зависит от LLM-`risk` поля intent-а — namespace/kind/replicas
    берутся из структурного intent-а; недоступные сигналы трактуются
    fail-closed (см. _apply_fail_closed_axes).
    """
    target = _intent_to_target(intent)
    spec = action_spec(intent.action)
    hint = {"command_kind": spec.command_kind or "unknown"}
    axes = compute_risk_axes(target, classification_signals=None, playbook_hint=hint)
    axes = _apply_fail_closed_axes(axes, intent)
    return evaluate_policy(_EXECUTOR_GATE_POLICY, axes, target=target)
