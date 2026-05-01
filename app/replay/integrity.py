from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.contracts.validator import validate_contract_payload

IntegrityStatus = Literal["PASS", "DEGRADED", "FAIL"]
ReplayMode = Literal["FULL", "LOW_FIDELITY", "BLOCKED"]


@dataclass(frozen=True)
class IntegrityGateResult:
    status: IntegrityStatus
    mode: ReplayMode
    reason: str


def evaluate_snapshot_integrity(snapshot_payload: dict) -> IntegrityGateResult:
    """Evaluate snapshot integrity and decide replay mode.

    Rules:
    - FAIL -> BLOCKED
    - DEGRADED -> LOW_FIDELITY
    - PASS -> FULL
    """
    validate_contract_payload("snapshot.v1", snapshot_payload)

    status = snapshot_payload.get("integrity_status", "PASS")
    if status == "FAIL":
        return IntegrityGateResult(status="FAIL", mode="BLOCKED", reason="snapshot integrity failed")
    if status == "DEGRADED":
        return IntegrityGateResult(status="DEGRADED", mode="LOW_FIDELITY", reason="snapshot degraded")
    return IntegrityGateResult(status="PASS", mode="FULL", reason="snapshot passed")
