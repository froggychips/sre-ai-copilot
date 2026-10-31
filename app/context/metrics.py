from kubernetes import client


class MetricsCollector:
    def __init__(self, k8s_client):
        self.k8s = k8s_client

    def get_namespace_health(self, namespace: str) -> dict:
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace)
        # status в схеме kube API необязателен: под без статуса не Running
        # и рестартов не имеет.
        statuses = [p.status for p in pods.items if p.status is not None]
        restarts: int = 0
        for st in statuses:
            for c in st.container_statuses or []:
                restarts += int(c.restart_count or 0)
        return {
            "total_pods": len(pods.items),
            "running": len([st for st in statuses if st.phase == "Running"]),
            "restarts": restarts,
        }
