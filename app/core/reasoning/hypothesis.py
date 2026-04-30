from pydantic import BaseModel, Field
from typing import List, Optional, Dict

class Hypothesis(BaseModel):
    hypothesis_id: str
    claim: str
    description: str
    
    # Store ONLY IDs, no computed state
    supporting_evidence_ids: List[str] = []
    missing_evidence_ids: List[str] = []
    contradiction_evidence_ids: List[str] = []
    
    # Status and iteration tracking only
    status: str = "pending"
    iteration_count: int = 0
