from typing import List, Dict, Any, Optional
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.engine.unified_state import SystemState
from app.core.reasoning.engine.stable_energy_system import StableEnergySystem

class InterpretationEngine:
    def __init__(self, state: SystemState):
        self.state = state
        self.system = StableEnergySystem()

    def rank_hypotheses(self, top_k: int = 3) -> List[Hypothesis]:
        """Ранжирует гипотезы на основе нормализованной энергии."""
        # Для каждой гипотезы проецируем состояние системы и считаем Confidence
        for h in self.state.hypotheses:
            h.confidence_score = self.system.compute_confidence(
                SystemState(graph=self.state.graph, hypotheses=[h], beliefs=self.state.beliefs)
            )

        return sorted(self.state.hypotheses, key=lambda h: h.confidence_score, reverse=True)[:top_k]

    def build_explanation(self, root_cause: Hypothesis) -> Dict[str, Any]:
        """Формирует RCA строго из E_norm."""
        return {
            "root_cause": root_cause.claim,
            "confidence": round(root_cause.confidence_score, 4),
            "supporting_evidence": root_cause.supporting_evidence_ids,
            "summary": root_cause.description
        }


    def build_explanation(self, root_cause: Hypothesis) -> Dict[str, Any]:
        """Формирует финальный RCA-отчет."""
        causal_path = []
        if root_cause.supporting_evidence_ids:
            causal_path = self.extract_causal_chain(root_cause.supporting_evidence_ids[0])
            
        return {
            "root_cause": root_cause.claim,
            "confidence": round(root_cause.confidence_score, 2),
            "supporting_evidence": root_cause.supporting_evidence_ids,
            "contradictions": root_cause.contradiction_evidence_ids,
            "causal_chain": causal_path,
            "summary": root_cause.description
        }
