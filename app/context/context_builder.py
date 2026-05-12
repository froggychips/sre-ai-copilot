import structlog
from kubernetes import config
from kubernetes.config.config_exception import ConfigException
from starlette.concurrency import run_in_threadpool

from app.context.deployments import DeploymentCollector
from app.context.logs import LogCollector
from app.context.metrics import MetricsCollector

logger = structlog.get_logger()


class ContextBuilder:
    def __init__(self):
        # In-cluster config — основной путь в k8s. Fallback на local kubeconfig
        # для dev/e2e. Если не доступны оба — продолжаем без K8s API (для
        # тестов и replay-режима, где K8s не нужен).
        try:
            config.load_incluster_config()
            self.k8s_available = True
        except ConfigException:
            try:
                config.load_kube_config()
                self.k8s_available = True
            except (ConfigException, FileNotFoundError):
                logger.warning("k8s_config_unavailable", fallback="no-k8s-context")
                self.k8s_available = False

        self.metrics = MetricsCollector(None)
        self.deps = DeploymentCollector()
        self.logs = LogCollector()

    async def build_context(self, incident: dict) -> dict:
        """Async-сборка контекста.

        Все K8s-коллекторы синхронные и зовут client.CoreV1Api() в блокирующем
        режиме — оборачиваем в threadpool, чтобы не лочить event loop в
        Celery-task-е (он крутит async, см. celery_worker._generate_reply_logic).
        """
        ns = incident.get("targets", [{}])[0].get("namespace", "default")
        pod = incident.get("targets", [{}])[0].get("pod", "")

        if not self.k8s_available:
            return {
                "incident": incident,
                "metrics": None,
                "deployments": [],
                "logs_summary": "k8s api unavailable",
            }

        metrics = await run_in_threadpool(self.metrics.get_namespace_health, ns)
        deployments = await run_in_threadpool(self.deps.get_recent_deployments, ns)
        logs_summary = (
            await run_in_threadpool(self.logs.get_summary, ns, pod)
            if pod
            else "No pod target"
        )

        return {
            "incident": incident,
            "metrics": metrics,
            "deployments": deployments,
            "logs_summary": logs_summary,
        }
