import time
from typing import Dict, Any, List
from datetime import datetime

class IncidentEvent:
    def __init__(self, source: str, event_type: str, data: Dict[str, Any]):
        self.source = source
        self.event_type = event_type
        self.data = data
        self.timestamp = datetime.utcnow()

class KubernetesEventAdapter:
    @staticmethod
    def from_k8s(raw_event: Dict[str, Any]) -> IncidentEvent:
        return IncidentEvent("kubernetes", raw_event["reason"], raw_event)

class PrometheusAdapter:
    @staticmethod
    def from_metric(metric_name: str, value: float) -> IncidentEvent:
        return IncidentEvent("prometheus", metric_name, {"value": value})
