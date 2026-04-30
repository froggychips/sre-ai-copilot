from kubernetes import client
import time

class MetricsCollector:
    def __init__(self, k8s_client):
        self.k8s = k8s_client

    def get_namespace_health(self, namespace: str) -> dict:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace)
        return {
            "total_pods": len(pods.items),
            "running": len([p for p in pods.items if p.status.phase == "Running"]),
            "restarts": sum([int(c.restart_count) for p in pods.items for c in (p.status.container_statuses or [])])
        }
