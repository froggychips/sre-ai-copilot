"""Remediation pipeline — Phase A foundation.

**Phase A scope (см. memory/project_remediation_pipeline_plan.md)**:
- Decision preview только. Executor отсутствует физически (не отключённый
  флаг — модуль `kubectl_executor` просто не реализован).
- Pipeline: candidate -> target_ref -> classification -> risk_axes ->
  policy_decision -> planned_command_preview.
- Outcome — `RemediationDecisionPreview` со статусом всегда
  `not_executable / preview_only`.

Phase B+ добавит dry-run, observe loop, approve queue, audit ledger и
таблицы `remediation_actions` / `_observations` / `_approvals`. В Phase A
используется единая таблица `kg_remediation_decisions`.
"""
from __future__ import annotations

from app.remediation.classifier import (Classification, ClassificationResult,
                                        classify)
from app.remediation.playbook import (Playbook, PlaybookValidationError,
                                      load_playbook, load_registry)
from app.remediation.policy import (PolicyDecision, PolicyMode,
                                    evaluate_policy)
from app.remediation.preview import (RemediationDecisionPreview,
                                     build_decision_preview)
from app.remediation.risk_axes import (BlastRadius, Confidence, DataPlane,
                                       Freshness, Idempotency, NamespaceTier,
                                       ResourceKind, Reversibility, RiskAxes,
                                       compute_risk_axes)
from app.remediation.target_resolver import TargetRef, resolve_target

__all__ = [
    "BlastRadius",
    "Classification",
    "ClassificationResult",
    "Confidence",
    "DataPlane",
    "Freshness",
    "Idempotency",
    "NamespaceTier",
    "Playbook",
    "PlaybookValidationError",
    "PolicyDecision",
    "PolicyMode",
    "RemediationDecisionPreview",
    "ResourceKind",
    "Reversibility",
    "RiskAxes",
    "TargetRef",
    "build_decision_preview",
    "classify",
    "compute_risk_axes",
    "evaluate_policy",
    "load_playbook",
    "load_registry",
    "resolve_target",
]
