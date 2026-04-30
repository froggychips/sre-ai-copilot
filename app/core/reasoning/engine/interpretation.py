from typing import List, Dict, Any, Optional
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.evidence import EvidenceGraph
from app.core.reasoning.engine.confidence_engine import ConfidenceEngine

class InterpretationEngine:
    def __init__(self, graph: EvidenceGraph, beliefs: Dict[str, float]):
        self.graph = graph
        self.beliefs = beliefs
        self.confidence_engine = ConfidenceEngine()

    def rank_hypotheses(self, hypotheses: List[Hypothesis], top_k: int = 3) -> List[Hypothesis]:
        """Ранжирует гипотезы, используя ConfidenceEngine."""
        for h in hypotheses:
            h.confidence_score = self.confidence_engine.calculate(h, self.beliefs, self.graph)

        return sorted(hypotheses, key=lambda h: h.confidence_score, reverse=True)[:top_k]

    def build_explanation(self, root_cause: Hypothesis) -> Dict[str, Any]:
        return {
            "root_cause": root_cause.claim,
            "confidence": round(root_cause.confidence_score, 2),
            "summary": root_cause.description
        }

    def select_root_cause(self, ranked_hypotheses: List[Hypothesis]) -> Optional[Hypothesis]:
        """Выбирает лучшую гипотезу с подтвержденным каузальным путем."""
        for h in ranked_hypotheses:
            if not h.contradiction_evidence_ids: # Нет критических противоречий
                return h
        return ranked_hypotheses[0] if ranked_hypotheses else None

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
