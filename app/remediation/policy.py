"""Policy evaluator: (Playbook, RiskAxes, target) -> PolicyDecision.

Priority (см. memory/project_remediation_pipeline_plan.md):
    1. `block.any` — абсолютный приоритет; любое match здесь → BLOCK.
    2. `auto` — все указанные условия должны совпасть.
    3. `approve` — все указанные условия должны совпасть.
    4. default `BLOCK` с reason `no_matching_policy` (не approve!).

Block-инварианты перебивают auto: если playbook говорит `auto:
namespace_tier: [dev,squad]` но target prod, и `block.any.namespace_tier:
[prod]` есть — победа block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from app.remediation.playbook import (Playbook, _ApproveSection,
                                      _AutoSection, _BlockAnySection)
from app.remediation.risk_axes import RiskAxes


class PolicyMode(str, Enum):
    AUTO = "auto"
    APPROVE = "approve"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyDecision:
    """Final decision + explainable reasons.

    `reasons` — список структурированных reason'ов, например
    `[{"axis": "namespace_tier", "value": "prod", "rule": "block.any"}]`.
    UI/embed читает их и рендерит «🔴 prod (block.any)».
    """
    mode: PolicyMode
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "reasons": list(self.reasons)}


# --- Helpers -------------------------------------------------------------


def _ensure_list(v: str | list[str] | None) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _axis_value(axes: RiskAxes, name: str) -> str | None:
    """Lookup axis value as string. Returns None если axis нет."""
    val = getattr(axes, name, None)
    if val is None:
        return None
    if isinstance(val, Enum):
        return val.value
    return str(val)


def _matches_axis_list(
    axes: RiskAxes,
    axis_name: str,
    allowed: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Check whether axes.{axis_name}.value ∈ allowed.

    Returns (ok, reason_dict). Reason всегда возвращается с structured
    keys для audit.
    """
    cur = _axis_value(axes, axis_name)
    ok = cur is not None and cur in allowed
    return ok, {
        "axis": axis_name,
        "value": cur,
        "expected": list(allowed),
        "ok": ok,
    }


def _evaluate_block_any(
    block_any: _BlockAnySection | None,
    axes: RiskAxes,
) -> list[dict[str, Any]]:
    """Вернуть список reasons если хоть один из block.any критериев trigger'ит.

    Empty list → block.any не сработал.
    """
    if block_any is None:
        return []
    triggered: list[dict[str, Any]] = []
    for axis_name in ("namespace_tier", "resource_kind", "data_plane",
                      "reversibility", "confidence"):
        allowed = getattr(block_any, axis_name)
        if not allowed:
            continue
        cur = _axis_value(axes, axis_name)
        # block.any — semantics: «axis IN list → block». Если value совпадает
        # со списком — triggered.
        if cur is not None and cur in allowed:
            triggered.append({
                "axis": axis_name,
                "value": cur,
                "matched": list(allowed),
                "rule": "block.any",
            })
    return triggered


def _evaluate_auto(
    auto: _AutoSection | None,
    axes: RiskAxes,
    target: Mapping[str, Any] | None,
) -> tuple[bool, list[dict[str, Any]]]:
    """All указанные axis-constraints должны совпасть (AND).

    Если auto секция вообще не задана → False (нельзя auto без explicit).
    """
    if auto is None:
        return False, [{"rule": "auto", "reason": "auto section absent"}]
    checks: list[dict[str, Any]] = []
    any_constraint = False

    if auto.namespace_tier is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "namespace_tier", auto.namespace_tier)
        r["rule"] = "auto"
        checks.append(r)
        if not ok:
            return False, checks
    if auto.blast_radius is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "blast_radius", auto.blast_radius)
        r["rule"] = "auto"
        checks.append(r)
        if not ok:
            return False, checks
    if auto.data_plane is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "data_plane", auto.data_plane)
        r["rule"] = "auto"
        checks.append(r)
        if not ok:
            return False, checks
    if auto.owner_kind is not None:
        any_constraint = True
        expected = _ensure_list(auto.owner_kind)
        ok = bool(
            target
            and (target.get("owner_kind") or "") in expected
        )
        checks.append({
            "axis": "owner_kind",
            "value": (target or {}).get("owner_kind"),
            "expected": expected,
            "ok": ok,
            "rule": "auto",
        })
        if not ok:
            return False, checks
    if auto.logs_captured_or_ttl is not None:
        any_constraint = True
        expected_bool: bool = auto.logs_captured_or_ttl
        # signal expected from target.labels / facts. Если нет — fail.
        labels = (target or {}).get("labels") or {}
        if isinstance(labels, dict):
            actual = (
                str(labels.get("logs_captured", "")).lower() == "true"
                or str(labels.get("ttl_seconds_after_finished", "")).strip() != ""
            )
        else:
            actual = False
        ok = (actual == expected_bool)
        checks.append({
            "axis": "logs_captured_or_ttl",
            "value": actual,
            "expected": expected_bool,
            "ok": ok,
            "rule": "auto",
        })
        if not ok:
            return False, checks

    if not any_constraint:
        # `auto: {}` — нельзя «auto всё подряд».
        return False, [{
            "rule": "auto",
            "reason": "auto section has no constraints",
        }]
    return True, checks


def _evaluate_approve(
    approve: _ApproveSection | None,
    axes: RiskAxes,
    target: Mapping[str, Any] | None,
) -> tuple[bool, list[dict[str, Any]]]:
    """All условия (AND). Если approve секция отсутствует → False."""
    if approve is None:
        return False, [{
            "rule": "approve",
            "reason": "approve section absent",
        }]
    checks: list[dict[str, Any]] = []
    any_constraint = False
    if approve.namespace_tier is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "namespace_tier", approve.namespace_tier)
        r["rule"] = "approve"
        checks.append(r)
        if not ok:
            return False, checks
    if approve.blast_radius is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "blast_radius", approve.blast_radius)
        r["rule"] = "approve"
        checks.append(r)
        if not ok:
            return False, checks
    if approve.data_plane is not None:
        any_constraint = True
        ok, r = _matches_axis_list(axes, "data_plane", approve.data_plane)
        r["rule"] = "approve"
        checks.append(r)
        if not ok:
            return False, checks
    if approve.owner_kind is not None:
        any_constraint = True
        expected = _ensure_list(approve.owner_kind)
        ok = bool(
            target
            and (target.get("owner_kind") or "") in expected
        )
        checks.append({
            "axis": "owner_kind",
            "value": (target or {}).get("owner_kind"),
            "expected": expected,
            "ok": ok,
            "rule": "approve",
        })
        if not ok:
            return False, checks
    if not any_constraint:
        return False, [{
            "rule": "approve",
            "reason": "approve section has no constraints",
        }]
    return True, checks


def evaluate_policy(
    playbook: Playbook,
    axes: RiskAxes,
    target: Mapping[str, Any] | None = None,
    classification_confidence: str | None = None,
) -> PolicyDecision:
    """Run priority chain: block.any -> auto -> approve -> default block.

    `classification_confidence` — отдельный axis, который перекрывает auto:
    weak confidence никогда не auto, даже если playbook говорит «auto in dev».
    """
    block_section = playbook.policy.block
    block_triggers = _evaluate_block_any(
        block_section.any if block_section else None, axes,
    )
    if block_triggers:
        return PolicyDecision(mode=PolicyMode.BLOCK, reasons=block_triggers)

    # Weak confidence → принудительный block (не auto/approve без human).
    # Это ОТДЕЛЬНЫЙ инвариант, не часть YAML — playbook-author не может
    # его обойти.
    if classification_confidence == "weak":
        return PolicyDecision(
            mode=PolicyMode.BLOCK,
            reasons=[{
                "rule": "block_low_confidence",
                "value": "weak",
                "reason": "classification confidence is weak",
            }],
        )

    auto_ok, auto_reasons = _evaluate_auto(playbook.policy.auto, axes, target)
    if auto_ok:
        return PolicyDecision(mode=PolicyMode.AUTO, reasons=auto_reasons)

    approve_ok, approve_reasons = _evaluate_approve(
        playbook.policy.approve, axes, target,
    )
    if approve_ok:
        return PolicyDecision(mode=PolicyMode.APPROVE, reasons=approve_reasons)

    # Default: BLOCK с reason no_matching_policy. Это НЕ approve.
    return PolicyDecision(
        mode=PolicyMode.BLOCK,
        reasons=[{
            "rule": "block_no_matching_policy",
            "reason": "neither auto nor approve criteria matched",
            "auto_failed": auto_reasons,
            "approve_failed": approve_reasons,
        }],
    )
