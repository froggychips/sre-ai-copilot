import re
import asyncio
import structlog
from kubernetes import client, config as k8s_config

logger = structlog.get_logger()


class K8sFacts:
    """Собирает верифицированные факты из кластера для Critic-агента."""

    @staticmethod
    async def collect(namespace: str) -> str:
        return await asyncio.get_event_loop().run_in_executor(
            None, K8sFacts._collect_sync, namespace
        )

    @staticmethod
    def _collect_sync(namespace: str) -> str:
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        results = []

        try:
            v1 = client.CoreV1Api()

            # Факт 1: все non-Running поды в namespace + restart counts
            pods = v1.list_namespaced_pod(namespace)
            unhealthy_pods = []
            for p in pods.items:
                if p.status.phase not in ("Running", "Succeeded"):
                    restarts = sum(
                        cs.restart_count
                        for cs in (p.status.container_statuses or [])
                    )
                    unhealthy_pods.append((p.metadata.name, p.status.phase, restarts))
            unhealthy_strs = [
                f"{name}: {phase} restarts={restarts}"
                for name, phase, restarts in unhealthy_pods
            ]
            results.append(
                f"Unhealthy pods in {namespace}: {unhealthy_strs if unhealthy_strs else 'none'}"
            )

            # Факт 2: сравнение с peer namespace
            peer = K8sFacts._peer_namespace(namespace)
            if peer is None and namespace.startswith("squad-"):
                logger.warning("k8s_facts_peer_namespace_unrecognized", namespace=namespace,
                               hint="namespace starts with 'squad-' but doesn't match squad-<N>-<suffix> pattern")
            if peer:
                try:
                    peer_pods = v1.list_namespaced_pod(peer)
                    peer_unhealthy = [
                        p.metadata.name
                        for p in peer_pods.items
                        if p.status.phase not in ("Running", "Succeeded")
                    ]
                    results.append(
                        f"Peer namespace {peer} unhealthy pods: "
                        f"{peer_unhealthy if peer_unhealthy else 'none (healthy)'}"
                    )
                except Exception as e:
                    logger.warning("k8s_facts_peer_unavailable", peer=peer, error=str(e))

            # Факт 3: логи для первых 3 unhealthy подов (tail 50)
            for pod_name, _, _ in unhealthy_pods[:3]:
                try:
                    logs = v1.read_namespaced_pod_log(
                        pod_name, namespace, tail_lines=50
                    )
                    if logs:
                        results.append(f"Logs {pod_name} (tail 50):\n{logs.strip()}")
                except Exception:
                    try:
                        logs = v1.read_namespaced_pod_log(
                            pod_name, namespace, tail_lines=50, previous=True
                        )
                        if logs:
                            results.append(f"Logs {pod_name} (previous, tail 50):\n{logs.strip()}")
                    except Exception as e:
                        logger.warning("k8s_facts_logs_unavailable", pod=pod_name, error=str(e))

        except Exception as e:
            logger.error("k8s_facts_collection_failed", namespace=namespace, error=str(e))
            return f"[k8s_facts unavailable: {e}]"

        return "\n".join(results)

    @staticmethod
    def _peer_namespace(namespace: str) -> str | None:
        """squad-1-kingdom2 → squad-2-kingdom2, squad-2-shared → squad-3-shared."""
        m = re.match(r'^(squad-)(\d+)(-.+)$', namespace)
        if m:
            n = int(m.group(2))
            peer_n = 2 if n != 2 else 3
            return f"{m.group(1)}{peer_n}{m.group(3)}"
        return None
