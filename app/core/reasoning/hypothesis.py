from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Dict
from .evidence import Contradiction

class Hypothesis(BaseModel):
    hypothesis_id: str
    parent_hypothesis_id: Optional[str] = None
    derived_from: List[str] = []
    
    claim: str
    description: str
    
    supporting_evidence_ids: List[str] = []
    missing_evidence_ids: List[str] = []
    contradiction_evidence_ids: List[str] = []
    
    contradictions: List[Contradiction] = []
    
    confidence_score: float = 0.5
    confidence_breakdown: Dict[str, float] = Field(default_factory=dict)
    
    verification_queries: List[dict] = []
    
    status: Literal["pending", "verifying", "confirmed", "rejected"] = "pending"
    iteration_count: int = 0
