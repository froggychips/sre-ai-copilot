class HypothesisGenerator:
    @staticmethod
    def generate(graph) -> list:
        hypotheses = []
        # Rule-based inference
        if any(e.type == "OOMKilled" for e in graph.events):
            hypotheses.append({"claim": "OOM", "evidence": ["OOMKilled"]})
        if any(e.type == "CrashLoop" for e in graph.events):
            hypotheses.append({"claim": "App Crash", "evidence": ["CrashLoop"]})
        return hypotheses

class Scorer:
    @staticmethod
    def score(hypothesis: dict, evidence: list) -> dict:
        """
        Deterministic scoring with breakdown for transparency.
        """
        base_score = 0.5
        evidence_boost = 0.0
        
        # Logic: boost score if we have more than 2 pieces of evidence
        if len(evidence) > 2:
            evidence_boost = 0.3
            
        total = min(base_score + evidence_boost, 1.0)
        
        return {
            "total": total,
            "breakdown": {
                "base_score": base_score,
                "evidence_count_boost": evidence_boost,
                "evidence_count": len(evidence)
            }
        }
