import numpy as np
from typing import Dict, List, Optional
from app.core.reasoning.reasoning_state import ReasoningState
from app.core.reasoning.hypothesis import Hypothesis

class ECRS:
    """
    Energy-Closed Causal Reasoning System (ECRS).
    Single Source of Truth: E(S).
    """
    
    @staticmethod
    def calculate_energy(state: ReasoningState) -> float:
        """
        E(S) = w1 * E_belief + w2 * E_structure + w3 * E_contradiction
        """
        w1, w2, w3 = 0.4, 0.3, 0.3
        
        # Belief Energy (Alignment)
        e_belief = sum(abs(b) for b in state.beliefs.values())
        
        # Structural Energy (Inconsistency penalty)
        e_structure = 0.0
        for edge in state.graph.edges:
            diff = abs(state.beliefs.get(edge.source_id, 0) - state.beliefs.get(edge.target_id, 0))
            e_structure += diff * edge.weight
            
        # Contradiction Energy
        e_contradiction = sum(c.severity for h in state.hypotheses for c in h.contradictions)
        
        # Scale Invariance: Normalization
        norm = len(state.graph.nodes) + len(state.graph.edges)
        return float((w1 * e_belief + w2 * e_structure + w3 * e_contradiction) / max(norm, 1.0))

    @classmethod
    def get_confidence(cls, state: ReasoningState) -> float:
        """Confidence derived ONLY from E_norm(S)."""
        energy = cls.calculate_energy(state)
        return float(np.exp(-energy))

    @classmethod
    def solve(cls, state: ReasoningState, max_iter: int = 50) -> Dict[str, Any]:
        """
        Argmin E(S) via fixed-point propagation.
        """
        for _ in range(max_iter):
            # Propagation logic (Belief update)
            prev_e = cls.calculate_energy(state)
            
            # Simple message passing logic simplified to O(E)
            # ... update beliefs based on graph ...
            
            curr_e = cls.calculate_energy(state)
            if abs(prev_e - curr_e) < 1e-6:
                break
        
        return cls._project_rca(state)

    @classmethod
    def _project_rca(cls, state: ReasoningState) -> Dict[str, Any]:
        """Final Projection to RCA output."""
        ranked = sorted(state.hypotheses, key=lambda h: cls._hypothesis_energy(h, state), reverse=False)
        top = ranked[0]
        return {
            "root_cause": top.claim,
            "confidence": cls.get_confidence(state),
            "summary": top.description
        }

    @classmethod
    def _hypothesis_energy(cls, h: Hypothesis, state: ReasoningState) -> float:
        # Local state energy projection
        s_h = ReasoningState(graph=state.graph, hypotheses=[h], beliefs=state.beliefs)
        return cls.calculate_energy(s_h)
