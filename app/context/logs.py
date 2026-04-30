from kubernetes import client

class LogCollector:
    def get_summary(self, namespace: str, pod_name: str) -> str:
        v1 = client.CoreV1Api()
        # Берем последние 50 строк
        try:
            logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)
            return logs[-500:] # Вернуть короткий срез для ИИ
        except:
            return "Could not fetch logs"
