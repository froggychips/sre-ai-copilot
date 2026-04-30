import numpy as np
from typing import Dict
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.evidence import EvidenceGraph

class ConfidenceEngine:
    @staticmethod
    def calculate(hypothesis: Hypothesis, beliefs: Dict[str, float], graph: EvidenceGraph) -> float:
        """
        Deterministic mapping: Beliefs + Structure -> Confidence [0, 1].
        confidence = sigmoid( Σ |belief| * weight - contradiction_penalty )
        """
        support_beliefs = [beliefs.get(eid, 0.0) for eid in hypothesis.supporting_evidence_ids]
        contradictions = graph.get_contradictions(hypothesis.hypothesis_id)
        
        # Aggregated signal
        signal = sum(abs(b) for b in support_beliefs)
        
        # Penalty for contradictions (normalized)
        penalty = sum(c.severity for c in hypothesis.contradictions)
        
        # Sigmoid fusion
        val = signal - penalty
        return float(1 / (1 + np.exp(-val)))
