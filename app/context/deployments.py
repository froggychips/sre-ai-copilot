"""K8s deployment/statefulset + control-plane helpers — best-effort live lookups.

Используется alert_enrichment для fallback на live API когда KG не дал
ready/desired. Все вызовы — best-effort, skip-on-error, **short timeout**:
embed-pipeline бюджет <500ms p95, не имеем права блокировать его на
flaky kube API.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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


def fetch_deployment_rollout_state(
    namespace: str,
    name: str,
    *,
    timeout_sec: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Снимок «идёт ли накат» у Deployment: реплики, условие Progressing, писатели.

    Нужен, чтобы отличить зависший накат от churn-а generation внешним
    контроллером (см. alert_enrichment.classify_generation_churn). Один GET:
    `managedFields` API отдаёт в обычном ответе (прячет их только kubectl).

    Возвращает dict или None при любой ошибке/таймауте — caller трактует None
    как «не знаю» и оставляет алерт громким. Никогда не бросает.
    """
    if not _load_k8s_once():
        return None
    try:
        dep = client.AppsV1Api().read_namespaced_deployment(
            name, namespace, _request_timeout=timeout_sec
        )
    except Exception as e:
        logger.warning(
            "rollout_state_fetch_failed",
            namespace=namespace, name=name, error=type(e).__name__,
        )
        return None
    try:
        status = dep.status
        progressing = next(
            (c for c in (status.conditions or []) if c.type == "Progressing"), None
        )
        writers = [
            {
                "manager": m.manager or "",
                "operation": m.operation or "",
                "subresource": getattr(m, "subresource", None) or "",
                "time": m.time,
            }
            for m in (dep.metadata.managed_fields or [])
        ]
        return {
            "desired": int(dep.spec.replicas or 0),
            "ready": int(status.ready_replicas or 0),
            "updated": int(status.updated_replicas or 0),
            "unavailable": int(status.unavailable_replicas or 0),
            "progressing_reason": progressing.reason if progressing else None,
            "progressing_updated_at": (
                progressing.last_update_time if progressing else None
            ),
            "revision": (dep.metadata.annotations or {}).get(
                "deployment.kubernetes.io/revision"
            ),
            "writers": writers,
        }
    except Exception as e:
        logger.warning(
            "rollout_state_parse_failed",
            namespace=namespace, name=name, error=type(e).__name__,
        )
        return None


# Namespace'ы DaemonSet'ов и агентов: их поды стоят на КАЖДОЙ ноде, поэтому на
# вопрос «чьи стенды на этой ноде» они не отвечают и только вытесняют ответ.
# Рендер сворачивает их в один счётчик. Сверено с `kubectl get ds -A`
# 23.09.2026: cattle-system, jupyter, kube-system, logging, metallb-system,
# monitoring; остальное — штатные системные ns кластера.
NODE_SYSTEM_NAMESPACES = frozenset({
    "calico-system",
    "cattle-system",
    "cert-manager",
    "ingress-nginx",
    "jupyter",
    "kube-node-lease",
    "kube-public",
    "kube-system",
    "local-path-storage",
    "logging",
    "metallb-system",
    "monitoring",
    "tigera-operator",
})


def fetch_node_namespaces(
    node: str,
    *,
    timeout_sec: float = 3.0,
) -> Optional[List[Dict[str, Any]]]:
    """Какие namespace'ы живут на ноде: live-список подов по `spec.nodeName`.

    Запрос дежурного (23.09.2026): в нодовом алерте видно имя ноды, но не
    видно, чьи стенды на ней сидят, — приходилось идти в kubectl. В графе
    привязки под→нода нет, поэтому источник — live API (у SA `sre-ai` есть
    `list pods` по всем namespace'ам).

    Возвращает `[{"namespace", "pods", "system"}]` по убыванию числа подов;
    завершённые поды (Succeeded/Failed — отработавшие миграции и job'ы) не
    считаются: память и CPU ноды они уже не занимают. `None` = «не знаю»
    (нет kube-config, таймаут, любая ошибка) — рендер обязан сказать, что
    данных нет, а не «на ноде пусто». Никогда не пробрасывает исключение;
    дедлайн — `_request_timeout` (см. комментарий в `fetch_live_replicas`).
    """
    if not node:
        return None
    # Шторм нодовых алертов (одно имя, N нод) приходит одной группой, а
    # параллельные вебхуки гоняют enrichment в пуле потоков. Поэтому:
    #  - кэш на ноду (30 с) и предохранитель: после сбоя API 30 с не ходим;
    #  - single-flight по ноде: одновременные промахи по ОДНОЙ ноде ждут
    #    первый запрос, а не шлют свой;
    #  - лок держится только на чтение/запись состояния, НЕ на время запроса:
    #    разные ноды идут параллельно, попадания в кэш не ждут чужой I/O, и
    #    задержка уведомления ограничена одним timeout_sec (ревью PR #420).
    with _node_ns_lock:
        hit, value = _node_ns_cached_or_down(node)
        if hit:
            return value
        flight = _node_ns_inflight.get(node)
        owner = flight is None
        if owner:
            flight = threading.Event()
            _node_ns_inflight[node] = flight
    assert flight is not None

    if not owner:
        flight.wait(timeout_sec + 1.0)
        with _node_ns_lock:
            return _node_ns_cached_or_down(node)[1]

    result: Optional[List[Dict[str, Any]]] = None
    try:
        result = _query_node_namespaces(node, timeout_sec)
    finally:
        with _node_ns_lock:
            if result is not None:
                _node_ns_cache[node] = (time.monotonic(), result)
            _node_ns_inflight.pop(node, None)
        flight.set()
    return result


def _node_ns_cached_or_down(node: str) -> Tuple[bool, Optional[List[Dict[str, Any]]]]:
    """Под `_node_ns_lock`. (True, снимок) — валидный кэш ноды; (True, None) —
    предохранитель взведён; (False, None) — надо идти в API. Сначала свой
    кэш, потом предохранитель: сбой по ДРУГОЙ ноде не прячет снимок этой."""
    now = time.monotonic()
    cached = _node_ns_cache.get(node)
    if cached and now - cached[0] < _NODE_NS_CACHE_TTL_SEC:
        return True, cached[1]
    if now < _node_ns_api_down_until:
        return True, None
    return False, None


def _query_node_namespaces(node: str, timeout_sec: float) -> Optional[List[Dict[str, Any]]]:
    """Сам запрос к API, без лока. None при любой ошибке (взводит предохранитель)."""
    if not _load_k8s_once():
        return None
    try:
        pods = client.CoreV1Api().list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node}",
            _request_timeout=timeout_sec,
        )
    except Exception as e:
        logger.warning("node_namespaces_fetch_failed", node=node, error=type(e).__name__)
        _trip_node_ns_breaker()
        return None

    counts: Dict[str, int] = {}
    for pod in pods.items or []:
        phase = getattr(pod.status, "phase", None) if pod.status else None
        if phase in ("Succeeded", "Failed"):
            continue
        ns = pod.metadata.namespace
        counts[ns] = counts.get(ns, 0) + 1
    return [
        {"namespace": ns, "pods": n, "system": ns in NODE_SYSTEM_NAMESPACES}
        for ns, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# Кэш стендов ноды и предохранитель API (см. fetch_node_namespaces). 30 с
# хватает, чтобы шторм по одной ноде и повторы группы AM не долбили API, и
# мало, чтобы список стендов не устарел для дежурного.
_NODE_NS_CACHE_TTL_SEC = 30.0
_node_ns_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_node_ns_inflight: Dict[str, threading.Event] = {}
_node_ns_api_down_until = 0.0
_node_ns_lock = threading.Lock()


def _trip_node_ns_breaker() -> None:
    global _node_ns_api_down_until
    with _node_ns_lock:
        _node_ns_api_down_until = time.monotonic() + _NODE_NS_CACHE_TTL_SEC


def reset_node_namespaces_cache() -> None:
    """Для тестов: кэш, предохранитель и in-flight — модульные."""
    global _node_ns_api_down_until
    with _node_ns_lock:
        _node_ns_cache.clear()
        _node_ns_inflight.clear()
        _node_ns_api_down_until = 0.0


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
