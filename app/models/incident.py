from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AlertManagerAlert(BaseModel):
    """Single alert as delivered by Prometheus AlertManager webhook v4."""

    status: str  # "firing" | "resolved"
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: str
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: str


class AlertManagerWebhook(BaseModel):
    """Payload shape posted by AlertManager to a webhook receiver."""

    version: str
    groupKey: str
    status: str
    receiver: Optional[str] = None
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = None
    alerts: List[AlertManagerAlert]


class Incident(BaseModel):
    """Internal canonical incident shape used by the agent pipeline."""

    incident_id: str
    severity: str
    status: str  # "firing" | "resolved"
    summary: str
    description: str = ""
    namespace: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    starts_at: str
    ends_at: Optional[str] = None
    generator_url: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)
    # Заполняется на ingestion из app.services.teamcity_service.incident_teamcity_context.
    # None = TC не сконфигурирован, namespace не маппится, или TC недоступен.
    teamcity_context: Optional[Dict[str, Any]] = None
    # >0 если алерт снова сработал после RESOLVED — счётчик циклов флаппинга.
    flap_count: int = 0

    @classmethod
    def from_alertmanager(cls, alert: AlertManagerAlert) -> "Incident":
        labels = alert.labels
        annotations = alert.annotations
        return cls(
            incident_id=alert.fingerprint,
            severity=labels.get("severity", "unknown"),
            status=alert.status,
            summary=annotations.get(
                "summary", labels.get("alertname", "unknown alert")
            ),
            description=annotations.get("description", ""),
            namespace=labels.get("namespace"),
            labels=labels,
            annotations=annotations,
            starts_at=alert.startsAt,
            ends_at=alert.endsAt,
            generator_url=alert.generatorURL,
            raw=alert.model_dump(),
        )


class AgentResponse(BaseModel):
    agent_name: str
    content: str
    metadata: Dict[str, Any] = {}


class FullAnalysisReport(BaseModel):
    incident_id: str
    summary: str
    hypotheses: List[str]
    critic_feedback: str
    suggested_fix: str
    kubectl_commands: List[str] = []
    risk_level: str
    requires_approval: bool
