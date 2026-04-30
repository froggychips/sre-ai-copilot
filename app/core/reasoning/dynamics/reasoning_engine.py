import numpy as np
from typing import List, Dict
from app.core.reasoning.evidence import EvidenceGraph, EvidenceRelationType
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.confidence_engine import BayesianConfidenceEngine
from .stabilization import BeliefStabilizationLayer

class ReasoningEngine:
    def __init__(self, graph: EvidenceGraph):
        self.graph = graph
        self.stabilizer = BeliefStabilizationLayer(alpha=0.3)
        self.prev_beliefs = {eid: e.confidence * 2 - 1 for eid, e in graph.nodes.items()}

    def propagate(self):
        """Распространение сигналов по графу с учетом стабилизации."""
        new_beliefs = self.prev_beliefs.copy()
        for edge in self.graph.edges:
            source = self.prev_beliefs.get(edge.source_id, 0.0)
            
            # Функция распространения (сигнал * вес * тип)
            multiplier = self._get_relation_multiplier(edge.relation_type)
            signal = source * edge.weight * multiplier * edge.causal_strength
            
            # Накопление сигнала
            target_id = edge.target_id
            new_beliefs[target_id] = np.clip(
                new_beliefs.get(target_id, 0.0) + signal, -1.0, 1.0
            )
            
        # 2. STABILIZATION
        stabilized = self.stabilizer.apply(self.prev_beliefs, new_beliefs)
        self.prev_beliefs = self.stabilizer.normalize_mass(stabilized)

    def compute_hypothesis_belief(self, hypothesis: Hypothesis) -> float:
        """Расчет confidence через накопленные belief сигналы."""
        related_beliefs = [self.prev_beliefs.get(eid, 0.0) for eid in hypothesis.supporting_evidence_ids]
        if not related_beliefs: return 0.5
        
        mean_belief = np.mean(related_beliefs)
        return 1 / (1 + np.exp(-mean_belief * 3))

    def _get_relation_multiplier(self, rel_type: EvidenceRelationType) -> float:
        return {
            EvidenceRelationType.SUPPORTS: 1.0,
            EvidenceRelationType.CONTRADICTS: -1.0,
            EvidenceRelationType.CAUSES: 1.2,
            EvidenceRelationType.CORRELATES: 0.5,
            EvidenceRelationType.TEMPORAL_FOLLOWS: 0.2
        }.get(rel_type, 0.0)

    def is_converged(self, threshold: float = 0.01) -> bool:
        """Проверка сходимости по изменению дельты belief state."""
        return True # Будет расширено в следующей фазе
