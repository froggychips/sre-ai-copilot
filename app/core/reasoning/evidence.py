from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, List, Optional, Dict

class EvidenceRelationType(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CAUSES = "causes"
    CORRELATES = "correlates"
    TEMPORAL_FOLLOWS = "temporal_follows"

class Evidence(BaseModel):
    evidence_id: str
    source: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    type: str
    value: Any
    confidence: float
    related_services: List[str] = []
    freshness_score: float = 1.0
    decay_rate: float = 0.01

class EvidenceRelation(BaseModel):
    source_id: str
    target_id: str
    relation_type: EvidenceRelationType
    weight: float = 0.5
    causal_strength: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class Contradiction(BaseModel):
    evidence_id: str
    conflicting_evidence_id: str
    severity: float
    explanation: str

class EvidenceGraph(BaseModel):
    nodes: Dict[str, Evidence] = Field(default_factory=dict)
    edges: List[EvidenceRelation] = Field(default_factory=list)

    def add_evidence(self, e: Evidence):
        self.nodes[e.evidence_id] = e

    def add_relation(self, relation: EvidenceRelation):
        self.edges.append(relation)

    def get_supporting(self, evidence_id: str) -> List[EvidenceRelation]:
        return [e for e in self.edges if e.target_id == evidence_id and e.relation_type == EvidenceRelationType.SUPPORTS]

    def get_contradictions(self, evidence_id: str) -> List[EvidenceRelation]:
        return [e for e in self.edges if e.target_id == evidence_id and e.relation_type == EvidenceRelationType.CONTRADICTS]

    def traverse_causal_chain(self, evidence_id: str) -> List[str]:
        # Простой BFS для поиска причинно-следственной цепочки
        chain = []
        queue = [evidence_id]
        while queue:
            current = queue.pop(0)
            chain.append(current)
            for edge in self.edges:
                if edge.target_id == current and edge.relation_type == EvidenceRelationType.CAUSES:
                    queue.append(edge.source_id)
        return chain
