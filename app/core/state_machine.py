from enum import Enum
from typing import Dict, Set


class IncidentState(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    # FACTS_COLLECTED — промежуточная стадия после DiagnosticEngine.run.
    # Введена с fact-anchored reasoning архитектурой (см. multi_hypothesis
    # + fact_critic). Legacy-pipeline без deterministic-слоя по-прежнему
    # может идти INVESTIGATING → HYPOTHESIS_GENERATED напрямую.
    FACTS_COLLECTED = "FACTS_COLLECTED"
    HYPOTHESIS_GENERATED = "HYPOTHESIS_GENERATED"
    FIX_PROPOSED = "FIX_PROPOSED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    EXECUTING = "EXECUTING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


class StateMachine:
    # Три валидных pipeline-пути:
    #
    # 1. **Fact-anchored** (новый, deterministic-first):
    #    OPEN → INVESTIGATING → FACTS_COLLECTED → HYPOTHESIS_GENERATED
    #         → FIX_PROPOSED → RESOLVED
    #
    # 2. **Legacy chain** (старый, без deterministic-слоя; сохранён как
    #    fallback и для backward-compat existing incident-ов):
    #    OPEN → INVESTIGATING → HYPOTHESIS_GENERATED → FIX_PROPOSED → RESOLVED
    #
    # 3. **With human approval** (любой из двух предыдущих + apply):
    #    ... → FIX_PROPOSED → APPROVAL_PENDING → EXECUTING → RESOLVED
    #
    # FIX_PROPOSED имеет два non-failure исхода: прямо в RESOLVED (только
    # synthesis-отчёт) или в APPROVAL_PENDING (apply через approval flow).
    # FAILED достижим из любого non-terminal state.
    TRANSITIONS: Dict[IncidentState, Set[IncidentState]] = {
        IncidentState.OPEN: {IncidentState.INVESTIGATING, IncidentState.FAILED},
        IncidentState.INVESTIGATING: {
            IncidentState.FACTS_COLLECTED,
            # Legacy direct path сохраняется — без него старые pipeline-ы
            # сломаются. После полной миграции можно будет удалить.
            IncidentState.HYPOTHESIS_GENERATED,
            IncidentState.FAILED,
        },
        IncidentState.FACTS_COLLECTED: {
            IncidentState.HYPOTHESIS_GENERATED,
            IncidentState.FAILED,
        },
        IncidentState.HYPOTHESIS_GENERATED: {
            IncidentState.FIX_PROPOSED,
            IncidentState.FAILED,
        },
        IncidentState.FIX_PROPOSED: {
            IncidentState.APPROVAL_PENDING,
            IncidentState.RESOLVED,
            IncidentState.FAILED,
        },
        IncidentState.APPROVAL_PENDING: {IncidentState.EXECUTING, IncidentState.FAILED},
        IncidentState.EXECUTING: {IncidentState.RESOLVED, IncidentState.FAILED},
        IncidentState.RESOLVED: set(),
        IncidentState.FAILED: set(),
    }

    @classmethod
    def validate_transition(
        cls, current: IncidentState, next_state: IncidentState
    ) -> bool:
        return next_state in cls.TRANSITIONS.get(current, set())
