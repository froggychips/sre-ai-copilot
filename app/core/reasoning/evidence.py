from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, List, Optional

class Evidence(BaseModel):
    evidence_id: str
    source: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    type: str
    value: Any
    confidence: float
    related_services: List[str] = []

class EvidenceGraph:
    def __init__(self):
        self.nodes: List[Evidence] = []
        self.edges: List[tuple] = []  # Список кортежей (id1, id2, relation)

    def add_evidence(self, e: Evidence, linked_to: Optional[str] = None):
        self.nodes.append(e)
        if linked_to:
            self.edges.append((linked_to, e.evidence_id, "linked_to"))
