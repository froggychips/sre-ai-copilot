class RCAExplainer:
    @staticmethod
    def explain(incident_id: str, best_hypothesis: dict, evidence: list) -> dict:
        return {
            "incident_id": incident_id,
            "root_cause": best_hypothesis["claim"],
            "confidence": best_hypothesis["score"],
            "evidence": [e.event_type for e in evidence],
            "explanation": f"The incident was likely caused by {best_hypothesis['claim']} based on observed events."
        }
