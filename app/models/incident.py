from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional

class NewRelicIncident(BaseModel):
    account_id: int
    account_name: str
    closed_violations_count: int
    condition_id: int
    condition_name: str
    current_state: str
    details: str
    event_type: str
    incident_acknowledge_url: str
    incident_id: int
    incident_url: str
    open_violations_count: int
    owner: str
    policy_name: str
    policy_url: str
    severity: str
    timestamp: int
    targets: List[Dict[str, Any]] = []

class AgentResponse(BaseModel):
    agent_name: str
    content: str
    metadata: Dict[str, Any] = {}

class FullAnalysisReport(BaseModel):
    incident_id: int
    summary: str
    hypotheses: List[str]
    critic_feedback: str
    suggested_fix: str
    kubectl_commands: List[str] = []
    risk_level: str
    requires_approval: bool
