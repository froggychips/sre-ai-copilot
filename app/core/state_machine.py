from enum import Enum
from typing import Dict, Set

class IncidentState(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    HYPOTHESIS_GENERATED = "HYPOTHESIS_GENERATED"
    FIX_PROPOSED = "FIX_PROPOSED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    EXECUTING = "EXECUTING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"

class StateMachine:
    # Two paths through the state machine, both valid:
    #
    # 1. **Analysis-only** (Celery worker, Synthesis-as-stage-6):
    #    OPEN → INVESTIGATING → HYPOTHESIS_GENERATED → FIX_PROPOSED → RESOLVED
    #    The pipeline writes a synthesised report and that's the end of the
    #    incident's lifecycle. No human approval, no kubectl apply.
    #
    # 2. **With human approval** (approvals API):
    #    OPEN → ... → FIX_PROPOSED → APPROVAL_PENDING → EXECUTING → RESOLVED
    #    A human approves the proposed fix and the executor applies it.
    #
    # FIX_PROPOSED therefore has two outgoing non-failure edges: straight
    # to RESOLVED (path 1) or into the approval pipeline (path 2).
    # FAILED is reachable from every non-terminal state.
    TRANSITIONS: Dict[IncidentState, Set[IncidentState]] = {
        IncidentState.OPEN: {IncidentState.INVESTIGATING, IncidentState.FAILED},
        IncidentState.INVESTIGATING: {IncidentState.HYPOTHESIS_GENERATED, IncidentState.FAILED},
        IncidentState.HYPOTHESIS_GENERATED: {IncidentState.FIX_PROPOSED, IncidentState.FAILED},
        IncidentState.FIX_PROPOSED: {IncidentState.APPROVAL_PENDING, IncidentState.RESOLVED, IncidentState.FAILED},
        IncidentState.APPROVAL_PENDING: {IncidentState.EXECUTING, IncidentState.FAILED},
        IncidentState.EXECUTING: {IncidentState.RESOLVED, IncidentState.FAILED},
        IncidentState.RESOLVED: set(),
        IncidentState.FAILED: set(),
    }

    @classmethod
    def validate_transition(cls, current: IncidentState, next_state: IncidentState) -> bool:
        return next_state in cls.TRANSITIONS.get(current, set())
