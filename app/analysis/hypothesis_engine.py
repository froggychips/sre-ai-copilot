class HypothesisGenerator:
    @staticmethod
    def generate(graph) -> list:
        """
        Generates a list of hypotheses with associated evidence and base confidence.
        """
        hypotheses = []
        
        # 1. Check for Memory Issues
        oom_events = [e for e in graph.events if e.type == "OOMKilled"]
        if oom_events:
            hypotheses.append({
                "name": "Memory Pressure / OOM",
                "confidence": 0.85,
                "evidence": [
                    "Container OOMKilled event detected",
                    f"Found {len(oom_events)} termination signals in 5m window",
                    "Pod restart count increased"
                ]
            })

        # 2. Check for Application Stability
        crash_events = [e for e in graph.events if e.type == "CrashLoopBackOff"]
        if crash_events:
            hypotheses.append({
                "name": "Application Runtime Failure",
                "confidence": 0.70,
                "evidence": [
                    "CrashLoopBackOff state observed",
                    "Logs indicate exit code 1 or panic",
                    "Startup/Liveness probe failing"
                ]
            })

        # 3. Check for Infrastructure / Scheduling
        sched_events = [e for e in graph.events if e.type == "FailedScheduling"]
        if sched_events:
            hypotheses.append({
                "name": "Resource Exhaustion (Node Level)",
                "confidence": 0.90,
                "evidence": [
                    "FailedScheduling events in namespace",
                    "Insufficient CPU/Memory on available nodes",
                    "Taints/Tolerations mismatch suspected"
                ]
            })

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
