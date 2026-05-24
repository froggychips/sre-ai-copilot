"""K8s deployment/statefulset helpers — best-effort live lookups.

Используется alert_enrichment для fallback на live API когда KG не дал
ready/desired. Все вызовы — best-effort, skip-on-error, **short timeout**:
embed-pipeline бюджет <500ms p95, не имеем права блокировать его на
flaky kube API.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional

import structlog
from kubernetes import client
from kubernetes import config as k8s_config

logger = structlog.get_logger("context.deployments")

_k8s_loaded = False


def _load_k8s_once() -> bool:
    """Загрузить kube-config один раз (in-cluster, иначе local).

    Возвращает True при успехе. False если ни одного из конфигов нет —
    в этом случае live-вызовы будут skip-нуты caller-ом.
    """
    global _k8s_loaded
    if _k8s_loaded:
        return True
    try:
        k8s_config.load_incluster_config()
        _k8s_loaded = True
        return True
    except Exception:
        pass
    try:
        k8s_config.load_kube_config()
        _k8s_loaded = True
        return True
    except Exception as e:
        logger.warning("k8s_config_unavailable", error=type(e).__name__)
        return False


class DeploymentCollector:
    def get_recent_deployments(self, namespace: str, limit=3) -> list:
        apps = client.AppsV1Api()
        deployments = apps.list_namespaced_deployment(namespace)
        # Сортируем по времени создания
        sorted_deps = sorted(
            deployments.items, key=lambda d: d.metadata.creation_timestamp, reverse=True
        )
        return [
            {
                "name": d.metadata.name,
                "created": str(d.metadata.creation_timestamp),
                "replicas": d.spec.replicas,
            }
            for d in sorted_deps[:limit]
        ]


def fetch_live_replicas(
    namespace: str,
    name: str,
    *,
    kind_hint: Optional[str] = None,
    timeout_sec: float = 3.0,
) -> Optional[Dict[str, int]]:
    """Live read ready/desired для Deployment или StatefulSet.

    `kind_hint`: "deployment" | "statefulset" | None — если передан,
    пробуем сразу нужный API; иначе пробуем оба (StatefulSet первым,
    т.к. live-issue чаще про БД/keeper-ы).

    Возвращает {ready, desired} или None при любой ошибке/таймауте.
    **Никогда** не пробрасывает исключение — caller рассчитывает на
    skip-on-error. Hard cap по сокету — `timeout_sec`.
    """
    if not _load_k8s_once():
        return None
    # Hard timeout на socket уровне. AppsV1Api не принимает per-call
    # timeout в k8s-client, поэтому через socket.setdefaulttimeout —
    # грубо, но достаточно для embed-budget.
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout_sec)
        apps = client.AppsV1Api()
        order: List[str] = []
        if kind_hint == "deployment":
            order = ["deployment", "statefulset"]
        elif kind_hint == "statefulset":
            order = ["statefulset", "deployment"]
        else:
            # Без hint — StatefulSet первым (типичный случай для
            # KubeStatefulSetReplicasMismatch — алёрт с этим именем).
            order = ["statefulset", "deployment"]
        for kind in order:
            try:
                if kind == "statefulset":
                    sts = apps.read_namespaced_stateful_set(name, namespace)
                    desired = int(sts.spec.replicas or 0)
                    ready = int(sts.status.ready_replicas or 0)
                    return {"ready": ready, "desired": desired}
                else:
                    dep = apps.read_namespaced_deployment(name, namespace)
                    desired = int(dep.spec.replicas or 0)
                    ready = int(dep.status.ready_replicas or 0)
                    return {"ready": ready, "desired": desired}
            except Exception:
                # Try next kind.
                continue
        return None
    except Exception as e:
        logger.warning(
            "live_replicas_fetch_failed",
            namespace=namespace, name=name, error=type(e).__name__,
        )
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


def fetch_last_log_line(
    namespace: str,
    pod_name: str,
    *,
    timeout_sec: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """TODO: skeleton — pull last log line + exit code from k8s API.

    Не реализовано в первой итерации (см. on-call note 10:38, item 5).
    `read_namespaced_pod_log` — самый дорогой и flaky API call;
    включать через `settings.INCLUDE_LAST_LOG_LINE` отдельно после
    канареечного прогона.

    Возвращает {line: str, exit_code: int|None} или None.
    """
    # Intentionally no-op. Скелет для будущей реализации.
    return None
