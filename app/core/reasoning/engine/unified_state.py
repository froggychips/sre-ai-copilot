from pydantic import BaseModel, Field
from typing import Dict, List
from app.core.reasoning.evidence import EvidenceGraph
from app.core.reasoning.hypothesis import Hypothesis

class SystemState(BaseModel):
    graph: EvidenceGraph
    hypotheses: List[Hypothesis]
    beliefs: Dict[str, float] = Field(default_factory=dict)

class UnifiedEnergyFunction:
    @staticmethod
    def compute(state: SystemState, w1: float = 0.4, w2: float = 0.3, w3: float = 0.3) -> float:
        # E(S) = w1 * E_belief + w2 * E_structure + w3 * E_contradiction
        e_belief = sum(abs(v) for v in state.beliefs.values())
        
        e_structure = 0.0
        for edge in state.graph.edges:
            # Penalty for inconsistencies in propagation
            e_structure += abs(state.beliefs.get(edge.source_id, 0) - state.beliefs.get(edge.target_id, 0))
            
        e_contradiction = sum(
            c.severity for h in state.hypotheses for c in h.contradictions
        )
        
        return float(w1 * e_belief + w2 * e_structure + w3 * e_contradiction)

class StateEvaluator:
    @staticmethod
    def evaluate(state: SystemState) -> float:
        return UnifiedEnergyFunction.compute(state)
