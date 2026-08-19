"""K8s deployment/statefulset + control-plane helpers — best-effort live lookups.

Используется alert_enrichment для fallback на live API когда KG не дал
ready/desired. Все вызовы — best-effort, skip-on-error, **short timeout**:
embed-pipeline бюджет <500ms p95, не имеем права блокировать его на
flaky kube API.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
    # Дедлайн на запрос задаётся ПЕР-ВЫЗОВ через `_request_timeout`,
    # который kubernetes-client пробрасывает в urllib3 (read/connect
    # timeout именно этого HTTP-запроса).
    #
    # НЕЛЬЗЯ ставить дедлайн через процесс-глобальный socket-таймаут
    # (socket.set/get default-timeout): это глобальная на весь процесс
    # настройка. Пайплайн гоняет инциденты конкурентно (Celery + asyncio
    # .gather в enrichment) → перекрывающиеся вызовы рейсятся, и finally
    # одного восстанавливает чужой timeout (или None), оставляя ВСЕ
    # прочие сокеты процесса (httpx к VM/Seq/Jira, DB, redis, k8s) с
    # неправильным/снятым таймаутом → спорадические зависания в
    # unrelated клиентах.
    try:
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
                    sts = apps.read_namespaced_stateful_set(
                        name, namespace, _request_timeout=timeout_sec
                    )
                    desired = int(sts.spec.replicas or 0)
                    ready = int(sts.status.ready_replicas or 0)
                    return {"ready": ready, "desired": desired}
                else:
                    dep = apps.read_namespaced_deployment(
                        name, namespace, _request_timeout=timeout_sec
                    )
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


# ── control-plane liveness ────────────────────────────────────────────────────

# scheduler и controller-manager держат leader-election Lease в kube-system и
# продлевают его раз в ~1-2 с. apiserver отдельного Lease не имеет, зато сам
# факт успешного ответа kube API и есть доказательство, что он жив.
_CP_LEASES: Dict[str, str] = {
    "kube-scheduler": "kube-scheduler",
    "kube-controller-manager": "kube-controller-manager",
}


def control_plane_component_alive(
    component: str,
    *,
    timeout_sec: float = 3.0,
    max_lease_age_sec: float = 60.0,
) -> Optional[bool]:
    """Жив ли компонент control-plane ПРЯМО СЕЙЧАС: True / False / None.

    `component`: "apiserver" | "kube-scheduler" | "kube-controller-manager".

    Зачем: алёрты `Kube{API,Scheduler,ControllerManager}Down` — это правила
    вида `absent(up{job=...})`, т.е. они срабатывают на ОТСУТСТВИЕ метрики.
    Метрика отсутствует и когда компонент упал, и когда ослеп сам мониторинг
    (vmagent потерял данные, scrape-gap). Различить эти два случая по самой
    метрике нельзя — нужен независимый источник, которым и служит kube API.

    `None` = «не знаю»: нет kube-config, таймаут, любая ошибка. Caller ОБЯЗАН
    трактовать None как «не подавлять» (fail-safe loud) — проспать реальное
    падение control-plane хуже лишнего пинга.

    Никогда не пробрасывает исключение. Hard cap по сокету — `timeout_sec`
    через `_request_timeout` (пер-вызов, не процесс-глобальный: почему именно
    так — см. развёрнутый комментарий в `fetch_live_replicas`).
    """
    if not _load_k8s_once():
        return None

    if component == "apiserver":
        try:
            # Самый дешёвый эндпоинт: /version. Ответил — apiserver обслуживает
            # запросы, значит `absent(up{job="apiserver"})` про слепоту скрейпа.
            client.VersionApi().get_code(_request_timeout=timeout_sec)
            return True
        except Exception as e:
            # Отличать «упал» от «сеть/RBAC» здесь нечем, поэтому не False, а
            # None: пусть алёрт останется громким.
            logger.warning("cp_liveness_apiserver_unknown", error=type(e).__name__)
            return None

    lease_name = _CP_LEASES.get(component)
    if not lease_name:
        return None
    try:
        lease = client.CoordinationV1Api().read_namespaced_lease(
            name=lease_name, namespace="kube-system", _request_timeout=timeout_sec
        )
        renew = getattr(lease.spec, "renew_time", None)
        if renew is None:
            return None
        age = (datetime.now(timezone.utc) - renew).total_seconds()
        # Свежий renewTime = лидер работает. Порог с большим запасом: штатный
        # интервал продления ~1-2 с, а leaseDurationSeconds по умолчанию 15.
        return age <= max_lease_age_sec
    except Exception as e:
        logger.warning(
            "cp_liveness_lease_unknown", component=component, error=type(e).__name__
        )
        return None
