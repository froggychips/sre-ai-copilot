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
    # Определяем допустимые переходы
    TRANSITIONS: Dict[IncidentState, Set[IncidentState]] = {
        IncidentState.OPEN: {IncidentState.INVESTIGATING, IncidentState.FAILED},
        IncidentState.INVESTIGATING: {IncidentState.HYPOTHESIS_GENERATED, IncidentState.FAILED},
        IncidentState.HYPOTHESIS_GENERATED: {IncidentState.FIX_PROPOSED, IncidentState.FAILED},
        IncidentState.FIX_PROPOSED: {IncidentState.APPROVAL_PENDING, IncidentState.FAILED},
        IncidentState.APPROVAL_PENDING: {IncidentState.EXECUTING, IncidentState.FAILED},
        IncidentState.EXECUTING: {IncidentState.RESOLVED, IncidentState.FAILED},
        IncidentState.RESOLVED: set(),
        IncidentState.FAILED: set(),
    }

    @classmethod
    def validate_transition(cls, current: IncidentState, next_state: IncidentState) -> bool:
        return next_state in cls.TRANSITIONS.get(current, set())
