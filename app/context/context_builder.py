from app.context.metrics import MetricsCollector
from app.context.deployments import DeploymentCollector
from app.context.logs import LogCollector
from kubernetes import config

class ContextBuilder:
    def __init__(self):
        config.load_incluster_config()
        self.metrics = MetricsCollector(None)
        self.deps = DeploymentCollector()
        self.logs = LogCollector()

    def build_context(self, incident: dict) -> dict:
        ns = incident.get("targets", [{}])[0].get("namespace", "default")
        pod = incident.get("targets", [{}])[0].get("pod", "")
        
        return {
            "incident": incident,
            "metrics": self.metrics.get_namespace_health(ns),
            "deployments": self.deps.get_recent_deployments(ns),
            "logs_summary": self.logs.get_summary(ns, pod) if pod else "No pod target"
        }
