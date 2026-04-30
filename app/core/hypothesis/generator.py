from typing import List
from app.core.graph.incident_graph import IncidentSignalGraph

class HypothesisGenerator:
    def generate(self, graph: IncidentSignalGraph) -> List[dict]:
        hypotheses = []
        # Простая детерминированная логика
        if any(e.event_type == "OOMKilled" for e in graph.nodes):
            hypotheses.append({"claim": "Memory saturation", "score": 0.8})
        if any(e.event_type == "FailedScheduling" for e in graph.nodes):
            hypotheses.append({"claim": "Resource exhaustion", "score": 0.7})
        return hypotheses
