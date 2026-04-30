from kubernetes import client

class K8sTopology:
    def __init__(self):
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()

    def get_service_map(self, namespace: str) -> dict:
        topo = {}
        deps = self.apps.list_namespaced_deployment(namespace).items
        for dep in deps:
            name = dep.metadata.name
            pods = self.core.list_namespaced_pod(namespace, label_selector=f"app={name}").items
            topo[name] = {
                "pods": [p.metadata.name for p in pods],
                "nodes": [p.spec.node_name for p in pods]
            }
        return topo
