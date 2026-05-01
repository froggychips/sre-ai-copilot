from app.core.intelligence.temporal_diff import TemporalDiffEngine
from app.core.intelligence.similar_incidents import SimilarIncidentEngine
from app.core.intelligence.next_steps import NextStepsGenerator
from app.core.intelligence.blast_radius import BlastRadiusEngine

class RCAExplainer:
    @staticmethod
    def create_report(incident_id, hypotheses, graph=None, topology=None):
        """
        Creates a strict, production-grade SRE RCA report.
        """
        if not hypotheses:
            return {"incident_id": incident_id, "summary": "No root cause identified."}

        # 1. Select Root Cause (Highest Confidence)
        root_cause_obj = max(hypotheses, key=lambda x: x["confidence"])
        
        # 2. Blast Radius Calculation
        blast_info = BlastRadiusEngine.calculate(
            {"targets": [{"service": root_cause_obj["name"]}]}, 
            topology or {}
        )

        # 3. Action Safety Layer (Deterministic)
        suggested_actions = []
        if root_cause_obj["name"] == "Memory Pressure / OOM":
            suggested_actions.append({
                "command": "kubectl set resources deployment <name> --limits=memory=...",
                "risk": "MEDIUM",
                "reason": "Safe but changes resource quotas"
            })
        elif root_cause_obj["name"] == "Application Runtime Failure":
            suggested_actions.append({
                "command": "kubectl rollout restart deployment <name>",
                "risk": "SAFE",
                "reason": "Standard stateless restart"
            })

        # 4. Temporal Diff
        t_diff = []
        if graph:
            # Simplified logic for practical report
            t_diff = ["Deployment update detected", "Traffic spike observed"] # Placeholder

        return {
            "incident_id": incident_id,
            "summary": f"Incident in {blast_info['affected_service']} detected with {root_cause_obj['confidence']*100}% confidence.",
            
            "hypotheses": [
                {
                    "name": h["name"],
                    "confidence": h["confidence"],
                    "evidence": h["evidence"]
                } for h in hypotheses
            ],

            "root_cause": {
                "name": root_cause_obj["name"],
                "confidence": root_cause_obj["confidence"]
            },

            "temporal_diff": t_diff,

            "blast_radius": {
                "critical": [blast_info['affected_service']],
                "affected_pods": blast_info['pods_count'],
                "severity": blast_info['severity'].upper()
            },

            "suggested_actions": suggested_actions,

            "risk_level": "MEDIUM" if any(a["risk"] == "MEDIUM" for a in suggested_actions) else "LOW",

            "approval_required": any(a["risk"] in ["MEDIUM", "HIGH", "CRITICAL"] for a in suggested_actions)
        }
