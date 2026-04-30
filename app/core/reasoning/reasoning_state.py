from pydantic import BaseModel
from typing import List
from .hypothesis import Hypothesis
from .evidence import EvidenceGraph

class ReasoningTraceNode(BaseModel):
    iteration: int
    hypothesis: str
    new_evidence: List[str]
    contradictions: List[str]
    confidence: float

class ReasoningState(BaseModel):
    hypotheses: List[Hypothesis] = []
    evidence_graph: EvidenceGraph = Field(default_factory=EvidenceGraph)
    contradictions_pool: List[dict] = []
    investigation_trace: List[ReasoningTraceNode] = []
    
    # Budget tracking
    max_iterations: int = 5
    iterations_used: int = 0
    max_tokens: int = 8000
    tokens_used: int = 0

    @classmethod
    def from_context(cls, context: dict):
        # Логика инициализации из контекста инцидента
        return cls()
