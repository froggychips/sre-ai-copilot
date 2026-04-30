class RCAExplainer:
    @staticmethod
    def create_report(incident_id, cause, score, timeline):
        return {
            "incident_id": incident_id,
            "root_cause": cause["claim"],
            "confidence": score,
            "timeline": timeline,
            "explanation": f"Detected {cause['claim']} based on {cause['evidence']}"
        }
