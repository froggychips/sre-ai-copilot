class RCAExplainer:
    @staticmethod
    def create_report(incident_id, cause, score_data, timeline):
        """
        Creates an explainable RCA report with confidence breakdown.
        """
        return {
            "incident_id": incident_id,
            "root_cause": cause["claim"],
            "confidence": score_data["total"],
            "confidence_breakdown": score_data["breakdown"],
            "timeline": timeline,
            "explanation": f"Detected {cause['claim']} based on {cause['evidence']}. "
                           f"Confidence is supported by {score_data['breakdown']['evidence_count']} signals."
        }
