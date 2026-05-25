"""End-to-end: alert + signals -> RemediationDecisionPreview.

Outcome — `RemediationDecisionPreview`, не action. Status всегда
`preview_only / not_executable`. Это **физическая граница** — модуль
`kubectl_executor` не реализован в Phase A.

Pipeline:
    alert + facts + signals
        -> target_resolver.resolve_target
        -> classifier.classify
        -> playbook matching (по classification + numeric match)
        -> risk_axes.compute_risk_axes
        -> policy.evaluate_policy
        -> render command_preview (НЕ exec)
        -> RemediationDecisionPreview
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app.remediation.classifier import (Classification, ClassificationResult,
                                        classify)
from app.remediation.playbook import (Playbook, _NumericConstraint,
                                      load_registry)
from app.remediation.policy import (PolicyDecision, PolicyMode, evaluate_policy)
from app.remediation.risk_axes import compute_risk_axes
from app.remediation.target_resolver import TargetRef, resolve_target


# Status — единственное допустимое значение в Phase A. Это **физическая
# граница** — executor не существует, не feature-flag.
EXECUTE_STATUS_PREVIEW_ONLY = "preview_only"


@dataclass
class RemediationDecisionPreview:
    """Полная картина решения copilot-а на данный alert.

    Никакая команда не запускается — `execute_status` всегда
    `preview_only`. UI/Discord embed читает поля для рендера.

    Поля:
    - `incident_id`: связка с IncidentRecord.
    - `alert_fingerprint`: cross-link AM (если есть).
    - `target_ref`: dict snapshot TargetRef.
    - `classification`: name класса (str).
    - `classification_provenance`: {rule_id, signals_used, confidence_hint}.
    - `risk_axes`: dict 8 axis enum values.
    - `candidate_playbooks`: список имён playbook-ов с matched `match`.
    - `selected_playbook`: имя playbook'а или None если кандидатов нет.
    - `decision`: 'auto' | 'approve' | 'block'.
    - `decision_reasons`: list structured reasons.
    - `command_preview`: shell-like представление команды (строка, НЕ run).
    - `idempotency_key`: для INSERT UNIQUE на (incident_id, idem_key).
    - `execute_status`: always 'preview_only'.
    """
    incident_id: str | None
    alert_fingerprint: str | None
    target_ref: dict[str, Any]
    classification: str
    classification_provenance: dict[str, Any]
    risk_axes: dict[str, str]
    candidate_playbooks: list[str] = field(default_factory=list)
    selected_playbook: str | None = None
    decision: str = "block"
    decision_reasons: list[dict[str, Any]] = field(default_factory=list)
    command_preview: str | None = None
    idempotency_key: str = ""
    execute_status: str = EXECUTE_STATUS_PREVIEW_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "alert_fingerprint": self.alert_fingerprint,
            "target_ref": dict(self.target_ref),
            "classification": self.classification,
            "classification_provenance": dict(self.classification_provenance),
            "risk_axes": dict(self.risk_axes),
            "candidate_playbooks": list(self.candidate_playbooks),
            "selected_playbook": self.selected_playbook,
            "decision": self.decision,
            "decision_reasons": list(self.decision_reasons),
            "command_preview": self.command_preview,
            "idempotency_key": self.idempotency_key,
            "execute_status": self.execute_status,
        }


# --- Helpers -------------------------------------------------------------


def _match_playbook(
    playbook: Playbook,
    classification: Classification,
    signals: Mapping[str, Any],
) -> bool:
    """Проверить `playbook.match` секцию против classification + signals."""
    if playbook.match.classification != classification.value:
        return False
    for axis_name, sig_key in (
        ("job_age_hours", "job_age_hours"),
        ("active_jobs", "active_jobs"),
        ("failed_jobs", "failed_jobs"),
        ("alert_age", "alert_age_minutes"),
        ("recent_deploy_age", "recent_deploy_age_minutes"),
        ("affected_replicas_pct", "affected_replicas_pct"),
    ):
        constraint: _NumericConstraint | None = getattr(
            playbook.match, axis_name, None,
        )
        if constraint is None:
            continue
        v_raw = signals.get(sig_key)
        try:
            v = float(v_raw) if v_raw is not None else None
        except (TypeError, ValueError):
            v = None
        if not constraint.evaluate(v):
            return False
    return True


def _render_command_preview(
    playbook: Playbook,
    target: TargetRef,
    extras: Mapping[str, Any] | None = None,
) -> str:
    """Substitute {placeholders} в plan.command, вернуть текстовое представление.

    NB: команда НЕ запускается. Возвращается человекочитаемая строка для
    UI/embed/audit.
    """
    ctx: dict[str, Any] = {
        "service": target.name or "",
        "name": target.name or "",
        "namespace": target.namespace or "",
        "ns": target.namespace or "",
        "job_name": target.name or "",
        "kind": target.kind or "",
        "owner_kind": target.owner_kind or "",
        "owner_name": target.owner_name or "",
    }
    if extras:
        for k, v in extras.items():
            if isinstance(k, str):
                ctx[k] = v

    rendered: list[str] = []
    for token in playbook.plan.command:
        try:
            rendered.append(token.format_map(_SafeDict(ctx)))
        except Exception:
            rendered.append(token)
    return " ".join(rendered)


class _SafeDict(dict):
    """str.format_map fallback: missing key -> `{key}` остаётся в выводе."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _compute_idempotency_key(
    incident_id: str | None,
    target: TargetRef,
    selected_playbook: str | None,
    classification: str,
) -> str:
    """Stable hash для UNIQUE constraint.

    Чтобы повтор той же alert'ы для того же target+playbook не плодил
    decision rows.
    """
    payload = "|".join([
        incident_id or "no-incident",
        target.namespace or "no-ns",
        target.kind or "no-kind",
        target.name or "no-name",
        selected_playbook or "no-playbook",
        classification,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _select_candidate_playbooks(
    registry: Mapping[str, Playbook],
    classification: ClassificationResult,
    signals: Mapping[str, Any],
) -> list[str]:
    """Список имён playbook-ов, у которых `match` сработал."""
    candidates: list[str] = []
    for name, pb in registry.items():
        if _match_playbook(pb, classification.classification, signals):
            candidates.append(name)
    return candidates


# --- Main entry ----------------------------------------------------------


def build_decision_preview(
    alert: Mapping[str, Any],
    facts: Mapping[str, Any] | None = None,
    signals: Mapping[str, Any] | None = None,
    kg_session: Any = None,
    registry: Mapping[str, Playbook] | None = None,
    incident_id: str | None = None,
) -> RemediationDecisionPreview:
    """Главная entry — alert + контекст -> RemediationDecisionPreview.

    Args:
        alert: dict с минимум `labels` (`namespace`, `pod`, `deployment`,
            etc.) и optional `fingerprint`.
        facts: enriched facts от alert-enrichment pipeline (target override).
        signals: classification signals (alert_age_minutes, failed_jobs, ...).
        kg_session: SQLA session для KG enrichment (read-only). Optional.
        registry: dict {name -> Playbook}. Default — `load_registry()`.
        incident_id: для записи в БД (Phase B); в preview только prop.

    Returns:
        RemediationDecisionPreview, всегда `execute_status='preview_only'`.
    """
    signals = signals or {}
    if registry is None:
        registry = load_registry()

    target = resolve_target(alert, facts=facts, kg_session=kg_session)
    classification_result = classify(target.to_dict(), signals)

    # Unknown target → block немедленно, без playbook matching.
    if target.unknown:
        axes = compute_risk_axes(target.to_dict(), signals)
        pv = RemediationDecisionPreview(
            incident_id=incident_id,
            alert_fingerprint=(alert or {}).get("fingerprint"),
            target_ref=target.to_dict(),
            classification=classification_result.classification.value,
            classification_provenance=classification_result.to_dict(),
            risk_axes=axes.to_dict(),
            candidate_playbooks=[],
            selected_playbook=None,
            decision=PolicyMode.BLOCK.value,
            decision_reasons=[{
                "rule": "block_unknown_target",
                "reason": "target cannot be resolved from alert/facts/kg",
                "resolved_via": target.resolved_via,
            }],
            command_preview=None,
            idempotency_key=_compute_idempotency_key(
                incident_id, target, None,
                classification_result.classification.value,
            ),
        )
        return pv

    # Кандидаты по `match`.
    candidates = _select_candidate_playbooks(
        registry, classification_result, signals,
    )

    # 0 кандидатов — block_no_matching_policy. Compute risk axes даже
    # без playbook, чтобы UI мог отрендерить axes-портрет.
    if not candidates:
        axes = compute_risk_axes(target.to_dict(), signals)
        return RemediationDecisionPreview(
            incident_id=incident_id,
            alert_fingerprint=(alert or {}).get("fingerprint"),
            target_ref=target.to_dict(),
            classification=classification_result.classification.value,
            classification_provenance=classification_result.to_dict(),
            risk_axes=axes.to_dict(),
            candidate_playbooks=[],
            selected_playbook=None,
            decision=PolicyMode.BLOCK.value,
            decision_reasons=[{
                "rule": "block_no_matching_policy",
                "reason": "no playbook matched this classification+signals",
                "classification": classification_result.classification.value,
            }],
            command_preview=None,
            idempotency_key=_compute_idempotency_key(
                incident_id, target, None,
                classification_result.classification.value,
            ),
        )

    # Выбираем первый кандидат (Phase A). В Phase B+ — ранжирование по
    # risk axes / playbook priority.
    selected_name = candidates[0]
    selected = registry[selected_name]

    # Hint для risk axes из playbook.plan.command — нужен idempotency/
    # reversibility.
    command_kind = _classify_command_kind(selected.plan.command)
    playbook_hint = {"command_kind": command_kind}

    axes = compute_risk_axes(
        target.to_dict(), signals, playbook_hint=playbook_hint,
    )

    policy: PolicyDecision = evaluate_policy(
        selected,
        axes,
        target=target.to_dict(),
        classification_confidence=classification_result.confidence_hint,
    )

    command_preview = _render_command_preview(selected, target)

    return RemediationDecisionPreview(
        incident_id=incident_id,
        alert_fingerprint=(alert or {}).get("fingerprint"),
        target_ref=target.to_dict(),
        classification=classification_result.classification.value,
        classification_provenance=classification_result.to_dict(),
        risk_axes=axes.to_dict(),
        candidate_playbooks=candidates,
        selected_playbook=selected_name,
        decision=policy.mode.value,
        decision_reasons=list(policy.reasons),
        command_preview=command_preview,
        idempotency_key=_compute_idempotency_key(
            incident_id, target, selected_name,
            classification_result.classification.value,
        ),
    )


def _classify_command_kind(command: Iterable[str]) -> str:
    """Heuristic: kubectl <verb> ... → verb string for risk_axes hint."""
    tokens = list(command)
    if len(tokens) >= 2 and tokens[0] == "kubectl":
        verb = tokens[1].lower()
        if verb == "delete":
            return "delete"
        if verb == "rollout":
            if len(tokens) >= 3 and tokens[2].lower() == "undo":
                return "rollout_undo"
            if len(tokens) >= 3 and tokens[2].lower() == "restart":
                return "restart"
        if verb == "scale":
            return "scale"
        if verb == "patch":
            return "patch_resources"
    return "unknown"
