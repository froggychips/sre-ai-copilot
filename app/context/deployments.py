from kubernetes import client

class DeploymentCollector:
    def get_recent_deployments(self, namespace: str, limit=3) -> list:
        apps = client.AppsV1Api()
        deployments = apps.list_namespaced_deployment(namespace)
        # Сортируем по времени создания
        sorted_deps = sorted(deployments.items, key=lambda d: d.metadata.creation_timestamp, reverse=True)
        return [
            {
                "name": d.metadata.name,
                "created": str(d.metadata.creation_timestamp),
                "replicas": d.spec.replicas
            } for d in sorted_deps[:limit]
        ]
