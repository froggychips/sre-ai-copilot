"""Rule-based classification (НЕ LLM).

LLM пишет narrative для Discord embed; classification — ключ policy и
playbook matching, поэтому должен быть детерминистичным. Result содержит
`signals_used` (для audit + provenance) и `rule_id`.

Доступные classes (расширяется по мере добавления playbook-ов):
- `unknown` — default, ни одно rule не сработало
- `stale_failed_job` — Job/CronJob с failed_count>0, active=0, age>24h
- `regression_post_deploy` — alert свежий + recent deploy < 30 мин
- `memory_pressure` — OOMKilled / mem_pct >= 90 / NodeMemoryPressure
- `chronic_unowned` — orphan service (нет owner) + chronic_score >= 0.5
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Classification(str, Enum):
    UNKNOWN = "unknown"
    STALE_FAILED_JOB = "stale_failed_job"
    REGRESSION_POST_DEPLOY = "regression_post_deploy"
    MEMORY_PRESSURE = "memory_pressure"
    CHRONIC_UNOWNED = "chronic_unowned"


@dataclass(frozen=True)
class ClassificationResult:
    """`class` + provenance (`rule_id`, `signals_used`).

    `signals_used` — те ключи signals, которые рулз reально читал, чтобы
    audit мог реконструировать решение.
    """
    classification: Classification
    rule_id: str
    signals_used: dict[str, Any] = field(default_factory=dict)
    confidence_hint: str = "medium"  # weak/medium/strong — feeds risk_axes

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "rule_id": self.rule_id,
            "signals_used": dict(self.signals_used),
            "confidence_hint": self.confidence_hint,
        }


# --- Rule helpers --------------------------------------------------------

def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rule_stale_failed_job(
    target: Mapping[str, Any],
    signals: Mapping[str, Any],
) -> ClassificationResult | None:
    """Job/CronJob с failed_count >= 1 и active = 0, age > 24h."""
    kind = (target.get("kind") or "").lower()
    if kind not in ("job", "cronjob"):
        return None
    failed = _to_int(signals.get("failed_jobs"))
    active = _to_int(signals.get("active_jobs"))
    age = _to_float(signals.get("job_age_hours"))
    if failed is None or active is None or age is None:
        return None
    if failed >= 1 and active == 0 and age >= 24:
        return ClassificationResult(
            classification=Classification.STALE_FAILED_JOB,
            rule_id="R1.stale_failed_job_v1",
            signals_used={
                "failed_jobs": failed,
                "active_jobs": active,
                "job_age_hours": age,
                "kind": kind,
            },
            confidence_hint="strong",
        )
    return None


def _rule_regression_post_deploy(
    target: Mapping[str, Any],
    signals: Mapping[str, Any],
) -> ClassificationResult | None:
    """Alert свежий (<= 60 мин) И recent deploy <= 30 мин назад."""
    alert_age = _to_float(signals.get("alert_age_minutes"))
    deploy_age = _to_float(signals.get("recent_deploy_age_minutes"))
    if alert_age is None or deploy_age is None:
        return None
    if alert_age <= 60 and deploy_age <= 30:
        return ClassificationResult(
            classification=Classification.REGRESSION_POST_DEPLOY,
            rule_id="R2.regression_post_deploy_v1",
            signals_used={
                "alert_age_minutes": alert_age,
                "recent_deploy_age_minutes": deploy_age,
            },
            confidence_hint="strong",
        )
    return None


def _rule_memory_pressure(
    target: Mapping[str, Any],
    signals: Mapping[str, Any],
) -> ClassificationResult | None:
    """OOMKilled / mem_pct >= 90 / NodeMemoryPressure."""
    reason = (signals.get("event_reason") or "").lower()
    if reason in ("oomkilled", "nodememorypressure", "memorypressure"):
        return ClassificationResult(
            classification=Classification.MEMORY_PRESSURE,
            rule_id="R3.memory_pressure_v1",
            signals_used={"event_reason": reason},
            confidence_hint="strong",
        )
    mem_pct = _to_float(signals.get("mem_pct"))
    if mem_pct is not None and mem_pct >= 90:
        return ClassificationResult(
            classification=Classification.MEMORY_PRESSURE,
            rule_id="R3.memory_pressure_v1",
            signals_used={"mem_pct": mem_pct},
            confidence_hint="medium",
        )
    return None


def _rule_chronic_unowned(
    target: Mapping[str, Any],
    signals: Mapping[str, Any],
) -> ClassificationResult | None:
    """Orphan (без owner) сервис с chronic_score >= 0.5 или stale_class chronic."""
    stale_class = (signals.get("stale_class") or "").lower()
    chronic_score = _to_float(signals.get("chronic_score"))
    has_owner = bool(target.get("owner_kind") or target.get("owner_name"))
    if has_owner:
        return None
    if stale_class in ("chronic", "suspicious_stale") or (
        chronic_score is not None and chronic_score >= 0.5
    ):
        return ClassificationResult(
            classification=Classification.CHRONIC_UNOWNED,
            rule_id="R4.chronic_unowned_v1",
            signals_used={
                "stale_class": stale_class or None,
                "chronic_score": chronic_score,
                "owner_known": has_owner,
            },
            confidence_hint="medium",
        )
    return None


# Order matters — first match wins. Specific (stale_failed_job, regression)
# идут раньше generic (memory_pressure, chronic_unowned), чтобы не свернуться
# в menos конкретный класс на тех же сигналах.
_RULES = (
    _rule_stale_failed_job,
    _rule_regression_post_deploy,
    _rule_memory_pressure,
    _rule_chronic_unowned,
)


def classify(
    target: Mapping[str, Any],
    signals: Mapping[str, Any] | None = None,
) -> ClassificationResult:
    """Apply rules в фиксированном порядке. Первый match выигрывает.

    Args:
        target: TargetRef.to_dict() — kind/owner_kind/owner_name/labels.
        signals: enriched signals dict — содержит ключи которые рулзы
            читают (alert_age_minutes, failed_jobs, mem_pct, ...).
    """
    s: Mapping[str, Any] = signals or {}
    for rule in _RULES:
        res = rule(target, s)
        if res is not None:
            return res
    return ClassificationResult(
        classification=Classification.UNKNOWN,
        rule_id="R0.no_match_v1",
        signals_used={},
        confidence_hint="weak",
    )
