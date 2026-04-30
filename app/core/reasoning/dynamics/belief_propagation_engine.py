import numpy as np
from typing import Dict, List
from app.core.reasoning.evidence import EvidenceGraph, EvidenceRelationType

class BeliefPropagationEngine:
    def __init__(self, graph: EvidenceGraph, epsilon: float = 1e-4, max_iter: int = 10):
        self.graph = graph
        self.epsilon = epsilon
        self.max_iter = max_iter
        self.beliefs = {eid: e.confidence * 2 - 1 for eid, e in graph.nodes.items()}

    def solve(self) -> Dict[str, float]:
        """O(E) propagation with delta-based convergence."""
        for _ in range(self.max_iter):
            prev = self.beliefs.copy()
            new_beliefs = self.beliefs.copy()
            
            # Message Passing
            for edge in self.graph.edges:
                multiplier = self._get_multiplier(edge.relation_type)
                signal = self.beliefs.get(edge.source_id, 0.0) * edge.weight * multiplier
                new_beliefs[edge.target_id] = np.clip(new_beliefs.get(edge.target_id, 0.0) + signal, -1.0, 1.0)
            
            self.beliefs = new_beliefs
            
            # Convergence check
            delta = max(abs(new_beliefs.get(k, 0) - prev.get(k, 0)) for k in self.beliefs)
            if delta < self.epsilon:
                break
        return self.beliefs

    def _get_multiplier(self, rel_type: EvidenceRelationType) -> float:
        return {
            EvidenceRelationType.SUPPORTS: 1.0,
            EvidenceRelationType.CONTRADICTS: -1.0,
            EvidenceRelationType.CAUSES: 1.0,
            EvidenceRelationType.CORRELATES: 0.5
        }.get(rel_type, 0.0)
