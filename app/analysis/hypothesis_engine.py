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
    def score(hypothesis: dict, evidence: list) -> float:
        # Simple heuristic, deterministic
        score = 0.5
        if len(evidence) > 2: score += 0.3
        return min(score, 1.0)
