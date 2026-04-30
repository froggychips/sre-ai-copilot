from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from .evidence import Evidence

class Hypothesis(BaseModel):
    hypothesis_id: str
    parent_hypothesis_id: Optional[str] = None
    derived_from: List[str] = []
    
    claim: str
    description: str
    supporting_evidence: List[str] = []
    missing_evidence: List[str] = []
    contradictions: List[str] = []
    
    confidence_score: float = 0.5
    verification_queries: List[dict] = [] # Typed plans
    
    status: Literal["pending", "verifying", "confirmed", "rejected"] = "pending"
    iteration_count: int = 0
