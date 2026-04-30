from typing import List, Dict, Any, Optional
from app.core.reasoning.hypothesis import Hypothesis
from app.core.reasoning.evidence import EvidenceGraph, EvidenceRelationType

class InterpretationEngine:
    def __init__(self, graph: EvidenceGraph, beliefs: Dict[str, float]):
        self.graph = graph
        self.beliefs = beliefs

    def rank_hypotheses(self, hypotheses: List[Hypothesis], top_k: int = 3, threshold: float = 0.2) -> List[Hypothesis]:
        """Ранжирует гипотезы по детерминированному скору."""
        for h in hypotheses:
            h.confidence_score = self._compute_score(h)
        
        ranked = [h for h in hypotheses if h.confidence_score >= threshold]
        return sorted(ranked, key=lambda h: h.confidence_score, reverse=True)[:top_k]

    def _compute_score(self, h: Hypothesis) -> float:
        """Determinstic scoring based on graph belief signals."""
        support = [self.beliefs.get(eid, 0.0) for eid in h.supporting_evidence_ids]
        contradictions = [self.beliefs.get(eid, 0.0) for eid in h.contradiction_evidence_ids]
        
        score = (
            (sum(support) / max(len(support), 1)) - 
            (sum(abs(c) for c in contradictions) / max(len(contradictions), 1)) +
            (len(h.supporting_evidence_ids) * 0.1) - 
            (len(h.missing_evidence_ids) * 0.05)
        )
        return float(np.clip(score, 0.0, 1.0))

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
