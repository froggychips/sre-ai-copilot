from app.core.intelligence.temporal_diff import TemporalDiffEngine
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
from app.core.intelligence.next_steps import NextStepsGenerator
from app.core.intelligence.blast_radius import BlastRadiusEngine

class RCAExplainer:
    @staticmethod
    def create_report(incident_id, cause, score_data, timeline, graph=None, topology=None):
        """
        Creates a production-grade explainable RCA report enriched with Intelligence layer.
        """
        root_cause = cause["claim"]
        
        # 1. Enrichment: Blast Radius
        blast_radius = BlastRadiusEngine.calculate(
            {"targets": [{"service": root_cause}]}, # Simplification for example
            topology or {}
        )

        # 2. Enrichment: Similar Incidents
        similar = SimilarIncidentEngine.find({"root_cause": root_cause})

        # 3. Enrichment: Next Steps
        next_steps = NextStepsGenerator.generate(root_cause)

        # 4. Enrichment: Temporal Diff (if graph is provided)
        t_diff = {}
        if graph:
            # For demo: comparing first half of events vs second half
            mid = len(graph.events) // 2
            t_diff = TemporalDiffEngine.compare(graph.events[:mid], graph.events[mid:])

        return {
            "incident_id": incident_id,
            "root_cause": root_cause,
            "confidence": score_data["total"],
            "confidence_breakdown": score_data["breakdown"],
            "timeline": timeline,
            "blast_radius": blast_radius,
            "similar_incidents": similar,
            "next_steps": next_steps,
            "temporal_diff": t_diff,
            "explanation": f"Incident identified as {root_cause}. "
                           f"Impacted service: {blast_radius['affected_service']} (Severity: {blast_radius['severity']})."
        }
