import json
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator

class ActionType(str, Enum):
    RESTART_DEPLOYMENT = "restart_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    GET_LOGS = "get_logs"
    DESCRIBE_RESOURCE = "describe_resource"
    GET_PODS = "get_pods"

class ExecutionIntent(BaseModel):
    action: ActionType
    resource_type: str = Field(..., pattern="^(deployment|pod|service|ingress)$")
    resource_name: str
    namespace: str = "default"
    params: Dict[str, Any] = Field(default_factory=dict)
    risk: str = "medium"

    @field_validator("namespace")
    @classmethod
    def block_system_ns(cls, v: str):
        if v in ["kube-system", "kube-public", "chaos-mesh"]:
            raise ValueError("Access to system namespaces via DSL is blocked.")
        return v

class DSLTranslator:
    @staticmethod
    def to_kubectl(intent: ExecutionIntent) -> str:
        """Детерминированная генерация команд на основе интента."""
        mapping = {
            ActionType.RESTART_DEPLOYMENT: f"kubectl rollout restart deployment/{intent.resource_name} -n {intent.namespace}",
            ActionType.SCALE_DEPLOYMENT: f"kubectl scale deployment/{intent.resource_name} -n {intent.namespace} --replicas={intent.params.get('replicas', 1)}",
            ActionType.GET_LOGS: f"kubectl logs {intent.resource_name} -n {intent.namespace} --tail=100",
            ActionType.DESCRIBE_RESOURCE: f"kubectl describe {intent.resource_type}/{intent.resource_name} -n {intent.namespace}",
            ActionType.GET_PODS: f"kubectl get pods -n {intent.namespace} -l {intent.params.get('label', '')}"
        }
        return mapping.get(intent.action)
