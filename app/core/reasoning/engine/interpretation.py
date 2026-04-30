from typing import List, Dict, Any, Optional
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.engine.unified_state import SystemState, StateEvaluator

class InterpretationEngine:
    def __init__(self, state: SystemState):
        self.state = state

    def rank_hypotheses(self, top_k: int = 3) -> List[Hypothesis]:
        """Ранжирует гипотезы на основе энергетического вклада в E(S)."""
        # Считаем вклад гипотезы в общую энергию как прокси confidence
        for h in self.state.hypotheses:
            h.confidence_score = self._compute_emergence(h)

        return sorted(self.state.hypotheses, key=lambda h: h.confidence_score, reverse=True)[:top_k]

    def _compute_emergence(self, h: Hypothesis) -> float:
        """Confidence как производная от энергетического ландшафта."""
        # Чем ниже E(h), тем выше стабильность (уверенность)
        # Инициализируем локальное состояние гипотезы для оценки
        local_state = SystemState(graph=self.state.graph, hypotheses=[h], beliefs=self.state.beliefs)
        energy = StateEvaluator.evaluate(local_state)
        return float(1 / (1 + energy))

    def build_explanation(self, root_cause: Hypothesis) -> Dict[str, Any]:
        """Формирует RCA строго из текущего SystemState."""
        return {
            "root_cause": root_cause.claim,
            "confidence": round(root_cause.confidence_score, 2),
            "supporting_evidence": root_cause.supporting_evidence_ids,
            "contradictions": root_cause.contradiction_evidence_ids,
            "summary": root_cause.description
        }

    def select_root_cause(self, ranked: List[Hypothesis]) -> Optional[Hypothesis]:
        return ranked[0] if ranked else None

    def extract_causal_chain(self, evidence_id: str) -> List[str]:
        """Использует traverse_causal_chain графа для RCA объяснения."""
        return self.graph.traverse_causal_chain(evidence_id)

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
