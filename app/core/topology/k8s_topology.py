from kubernetes import client

class K8sTopology:
    def __init__(self):
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()

    def get_service_map(self, namespace: str) -> dict:
        """Строит map зависимостей: Deployment -> Pods -> Nodes."""
        topo = {}
        deployments = self.apps.list_namespaced_deployment(namespace)
        for dep in deployments.items:
            name = dep.metadata.name
            label_selector = ",".join([f"{k}={v}" for k, v in dep.spec.selector.match_labels.items()])
            pods = self.core.list_namespaced_pod(namespace, label_selector=label_selector)
            
            topo[name] = {
                "pods": [p.metadata.name for p in pods.items],
                "nodes": list(set([p.spec.node_name for p in pods.items])),
                "replicas": dep.spec.replicas
            }
        return topo
