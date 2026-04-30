from typing import List, Dict
from app.ingestion.kubernetes_events import IncidentEvent

class IncidentSignalGraph:
    def __init__(self):
        self.nodes: List[IncidentEvent] = []
        self.edges: List[Dict] = []

    def add_event(self, event: IncidentEvent):
        self.nodes.append(event)
        # Автоматическая корреляция по времени
        for node in self.nodes:
            if abs((node.timestamp - event.timestamp).total_seconds()) < 300: # 5 min window
                self.edges.append({"source": node, "target": event, "type": "time_proximity"})
