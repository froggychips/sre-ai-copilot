"""Remediation: детерминированный путь «правило → playbook → policy → кнопка».

Начиналось (Phase A) как decision preview без исполнения. Сейчас:
- playbook-и — `app/remediation/registry/*.yaml` (v1 — только preview,
  v2 — исполнимые, с `plan.steps`), загрузка/валидация — `playbook.py`,
  выбор — `matcher.py`, политика — `policy.py` / `executor_gate.py`;
- мост `bridge.py` + `bridge_dispatch.py` (Celery-задача
  `remediation_bridge`, с 1.0.31/1.0.32) строит intent на сервере и при
  `approve` постит карточку с кнопками; запись — через executor
  (`app/services/executor_apply.py`) после клика оператора из allowlist;
- след решений и попыток — `kg_remediation_decisions` /
  `kg_remediation_attempts`, проверка исхода — `verify_checks.py`.

Подробно — docs/REMEDIATION_PLAYBOOK_CONTRACT.md и docs/ARCHITECTURE.md.
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
