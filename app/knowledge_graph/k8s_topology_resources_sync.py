"""Wave 7 / G1.3: declarative parser k8s Service + Ingress resources.

Закрывает главный gap источника истины для топологии: до сих пор edges
строятся в основном из env-var heuristic (см. `kg_sync.sync_topology`)
плюс narrow Ingress-host externalisation (`k8s_ingress_sync`). Этот
модуль — **golden middle**: дешевле eBPF/service-mesh, надёжнее env-vars.

Что добавляется:

* **Service** resources (`core/v1`):
  - upsert kg_services с `metadata_json` (service_type, ports, selector).
  - НЕ создаёт synthetic — если Service торчит без матчей на Deployment,
    его всё равно полезно иметь как node (downstream может на него
    routesить ingress).
  - edge `serves_traffic`: Service → backing Deployment (через selector
    match на pod template labels). Это «декларативная замена» Endpoint
    runtime-resolve — мы матчим на статичный deploy спек, не на pods.

* **Ingress** resources (`networking.k8s.io/v1`):
  - edge `routes_to`: Ingress (synthetic node `ingress:<name>` если ещё
    нет) → backend Service. Существующий `k8s_ingress_sync` создаёт
    node `ingress:<host>` с edge `calls` на backend, что хорошо для
    «external traffic entry», но не покрывает Ingress-as-resource
    (один ingress может иметь N hosts/paths). Этот модуль добавляет
    параллельный declarative slice — оба источника пишутся в edges с
    разным `discovered_by`, и `populator.upsert_edge` мерджит
    `discovery_sources` корректно.

Beat task: `kg_topology_resources_sync` каждые 15 минут.

CLI:
    python -m app.knowledge_graph.k8s_topology_resources_sync          # все ns
    python -m app.knowledge_graph.k8s_topology_resources_sync prod-shared
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import Service

logger = logging.getLogger(__name__)

# Default kubectl timeout. 30с не хватало: на этом кластере
# `kubectl get deployments -A -o json` — это 2318 объектов и ~42 МБ
# pretty-JSON, и внутри пода воркера (занятого kg_*-синками) запрос не
# укладывался. Итог: services_fetched=0 КАЖДЫЙ тик, реальные Service-узлы
# в граф не попадали вовсе → serves_traffic замер на 3 рёбрах, а routes_to
# терял backend-матчи (`skipped_no_backend_match`), т.е. новые окружения
# висели в KG как «topology unknown».
_KUBECTL_TIMEOUT_S = 180

# Пагинация листа: apiserver отдаёт по _KUBECTL_CHUNK объектов за запрос,
# kubectl склеивает. Пиковая стоимость одного round-trip падает на порядок,
# и большой лист больше не упирается в один таймаут.
_KUBECTL_CHUNK = 500

# Фоллбэк, когда даже чанкованный cluster-wide лист не успел: обходим
# namespace по одному (замер: 0.55с и ~480КБ на namespace против 42МБ на
# весь кластер). Медленнее по сумме, зато сбой одного ns не обнуляет тик.
_KUBECTL_TIMEOUT_NS_S = 20

# Edge kinds, которые этот модуль использует. ServiceEdge.kind — свободная
# строка, валидация на app-уровне. Эти константы — единственная sсемантика.
# Источник истины — `app.knowledge_graph.contract.EDGE_KINDS` (status='active').
EDGE_SERVES_TRAFFIC = "serves_traffic"   # Service → backing Deployment
EDGE_ROUTES_TO = "routes_to"             # Ingress → Service

DISCOVERED_BY_SVC = "k8s_topology_resources/service"
DISCOVERED_BY_INGRESS = "k8s_topology_resources/ingress"


# ── kubectl wrappers ────────────────────────────────────────────────────────


def _kubectl_namespaces() -> List[str]:
    """Список namespace'ов кластера (или [] при ошибке)."""
    try:
        out = subprocess.run(
            ["kubectl", "get", "namespaces", "-o",
             "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_NS_S,
        )
    except Exception as e:
        logger.warning("k8s_topology_resources.ns_list_failed err=%s", e)
        return []
    if out.returncode != 0:
        logger.warning(
            "k8s_topology_resources.ns_list_failed rc=%d stderr=%s",
            out.returncode, (out.stderr or "").strip()[:200],
        )
        return []
    return [ns for ns in (out.stdout or "").split("\n") if ns.strip()]


def _kubectl_get_per_namespace(resource: str) -> List[Dict[str, Any]]:
    """Фоллбэк: собрать `resource` обходом namespace по одному.

    Дороже по сумме round-trip'ов, но каждый запрос мелкий (замер на этом
    кластере: 0.55с / ~480КБ против 42МБ на весь кластер), поэтому лист
    целиком не проваливается из-за одного таймаута. Сбойный namespace
    пропускаем — частичные данные лучше пустого тика.
    """
    namespaces = _kubectl_namespaces()
    if not namespaces:
        return []
    items: List[Dict[str, Any]] = []
    failed = 0
    for ns in namespaces:
        try:
            out = subprocess.run(
                ["kubectl", "get", resource, "-n", ns, "-o", "json",
                 f"--chunk-size={_KUBECTL_CHUNK}"],
                capture_output=True, text=True, check=False,
                timeout=_KUBECTL_TIMEOUT_NS_S,
            )
            if out.returncode != 0:
                failed += 1
                continue
            items.extend((json.loads(out.stdout or "{}") or {}).get("items") or [])
        except Exception:
            failed += 1
            continue
    logger.info(
        "k8s_topology_resources.per_namespace_fallback resource=%s ns=%d items=%d failed_ns=%d",
        resource, len(namespaces), len(items), failed,
    )
    return items


def _kubectl_get_all(resource: str) -> List[Dict[str, Any]]:
    """`kubectl get <resource> -A -o json` → items list (или [] при ошибке).

    Не raise: failure в одном tick'е не должна валить beat-loop. Логируем
    warning со stderr-фрагментом и идём дальше.

    Лист запрашивается чанками (`--chunk-size`), а при таймауте всего листа
    подхватывает per-namespace фоллбэк — иначе один медленный ответ
    apiserver'а обнулял весь тик (см. комментарий к _KUBECTL_TIMEOUT_S).
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", resource, "-A", "-o", "json",
             f"--chunk-size={_KUBECTL_CHUNK}"],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "k8s_topology_resources.kubectl_timeout resource=%s → per-namespace fallback",
            resource,
        )
        return _kubectl_get_per_namespace(resource)
    except Exception as e:
        logger.warning(
            "k8s_topology_resources.kubectl_exception resource=%s err=%s",
            resource, e,
        )
        return []

    if out.returncode != 0:
        logger.warning(
            "k8s_topology_resources.kubectl_failed resource=%s rc=%d stderr=%s",
            resource, out.returncode, (out.stderr or "").strip()[:200],
        )
        return []

    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as e:
        logger.warning(
            "k8s_topology_resources.json_decode_failed resource=%s err=%s",
            resource, e,
        )
        return []

    items = data.get("items") or []
    return items if isinstance(items, list) else []


def _kubectl_get_deployments_all() -> List[Dict[str, Any]]:
    """Все deployments cluster-wide. Нужны чтобы по selector сматчить Service →
    backing Deployment. Кэшируем результат в течение одного tick'а (передаётся
    в sync_all_services как аргумент).
    """
    return _kubectl_get_all("deployments")


# ── pure helpers ────────────────────────────────────────────────────────────


def _extract_service_meta(svc: Dict[str, Any]) -> Dict[str, Any]:
    """Из Service JSON собрать metadata_json subset (для upsert_service).

    Берём то что полезно для запросов / debug:
      - service_type (ClusterIP / NodePort / LoadBalancer / ExternalName)
      - cluster_ip
      - ports (list of dicts: name/port/targetPort/protocol)
      - selector (dict labels)
    Игнорируем status (runtime — не declarative).
    """
    spec = svc.get("spec") or {}
    return {
        "service_type": spec.get("type"),
        "cluster_ip": spec.get("clusterIP"),
        "ports": spec.get("ports") or [],
        "selector": spec.get("selector") or {},
    }


def _selector_matches_labels(
    selector: Dict[str, str],
    labels: Dict[str, str],
) -> bool:
    """Equality-based selector match. k8s Service.spec.selector — всегда
    equality (matchLabels-style), без matchExpressions (это уже Deployment-level).

    Пустой selector — НЕ матчит ничего (k8s headless или ExternalName).
    Это намеренный return False: иначе сматчили бы все deployments в ns.
    """
    if not selector:
        return False
    for k, v in selector.items():
        if labels.get(k) != v:
            return False
    return True


def _find_matching_deployments(
    selector: Dict[str, str],
    namespace: str,
    deployments_index: Dict[str, List[Dict[str, Any]]],
) -> List[str]:
    """Вернуть имена Deployment'ов в `namespace`, чьи pod template labels
    удовлетворяют `selector`.

    `deployments_index` — map ns → list of deployments (предсобран caller'ом
    чтобы за tick дёрнуть kubectl один раз, не N).
    """
    if not selector:
        return []
    matches: List[str] = []
    for dep in deployments_index.get(namespace, []):
        meta = dep.get("metadata") or {}
        pod_labels = (
            ((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}
        ).get("labels") or {}
        if _selector_matches_labels(selector, pod_labels):
            dep_name = meta.get("name")
            if dep_name:
                matches.append(dep_name)
    return matches


def _index_deployments_by_ns(
    deployments: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Группировка deployments по namespace для O(1) lookup в matching."""
    idx: Dict[str, List[Dict[str, Any]]] = {}
    for dep in deployments:
        ns = ((dep.get("metadata") or {}).get("namespace")) or "default"
        idx.setdefault(ns, []).append(dep)
    return idx


def _extract_ingress_routes(
    ing: Dict[str, Any],
) -> List[Tuple[Optional[str], str, str]]:
    """Из Ingress spec вытащить routes как (host, path, backend_svc_name).

    Поддерживаем networking.k8s.io/v1 (`backend.service.name`). Старый
    `extensions/v1beta1` и v1beta1 networking — out of scope (cluster
    уже на v1 много версий).

    `defaultBackend` тоже учитываем (host=None, path="/*").
    """
    routes: List[Tuple[Optional[str], str, str]] = []
    spec = ing.get("spec") or {}

    db = spec.get("defaultBackend") or {}
    db_svc = (db.get("service") or {}).get("name")
    if db_svc:
        routes.append((None, "/*", db_svc))

    for rule in spec.get("rules") or []:
        host = rule.get("host")  # может быть None у wildcard
        http = rule.get("http") or {}
        for path in http.get("paths") or []:
            backend = path.get("backend") or {}
            svc = (backend.get("service") or {}).get("name")
            if svc:
                routes.append((host, path.get("path") or "/", svc))
    return routes


# ── service sync ────────────────────────────────────────────────────────────


def sync_all_services(
    db: Session,
    deployments_index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, int]:
    """Загрузить все Services cluster-wide, upsert nodes + edges.

    Возвращает stats:
        services_fetched      — сколько Service объектов получено из kubectl
        nodes_upserted        — сколько kg_services touched (create/update)
        edges_serves_traffic  — сколько Service→Deployment edges
        skipped_no_selector   — Services без selector (headless/ExternalName)
        skipped_no_match      — selector не сматчил ни одного Deployment
        skipped_self_loop     — Service≡Deployment одноимённый узел (self-edge)
    """
    services = _kubectl_get_all("services")
    if deployments_index is None:
        deployments_index = _index_deployments_by_ns(_kubectl_get_deployments_all())

    stats = {
        "services_fetched": len(services),
        "nodes_upserted": 0,
        "edges_serves_traffic": 0,
        "skipped_no_selector": 0,
        "skipped_no_match": 0,
        "skipped_self_loop": 0,
    }

    for svc in services:
        meta = svc.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        name = meta.get("name")
        if not name:
            continue

        meta_json = _extract_service_meta(svc)
        # upsert Service node. team_owner мы НЕ заполняем здесь — это
        # делает kg_sync на основе ns-pattern (squad-N → squad). Если
        # upsert_service видит существующий узел — он сохранит уже
        # выставленный team_owner и просто обновит metadata_json.
        svc_node = upsert_service(
            db,
            namespace=ns,
            name=name,
            metadata={"k8s_service": meta_json},
        )
        stats["nodes_upserted"] += 1

        selector = meta_json["selector"] or {}
        if not selector:
            stats["skipped_no_selector"] += 1
            continue

        matches = _find_matching_deployments(selector, ns, deployments_index)
        if not matches:
            stats["skipped_no_match"] += 1
            continue

        for dep_name in matches:
            dep_node = (
                db.query(Service)
                .filter_by(namespace=ns, name=dep_name)
                .one_or_none()
            )
            if dep_node is None:
                # Deployment видим в k8s, но в KG ещё нет — kg_topology_sync
                # подхватит его на следующем ежечасном тике. Не создаём
                # фантом-узлы здесь — это путь к bloat'у.
                continue
            if dep_node.id == svc_node.id:
                # Service и его Deployment одноимённы (Service `foo` бэкает
                # Deployment `foo`) → это ОДИН и тот же kg_services-узел (граф
                # ключует по name+namespace без разделителя типа). serves_traffic
                # сам-на-себя бессмыслен и засоряет blast-radius (сервис
                # показывает сам себя в IN-edges «кто пострадает»). Пропускаем.
                stats["skipped_self_loop"] += 1
                continue
            upsert_edge(
                db,
                src=svc_node,
                dst=dep_node,
                kind=EDGE_SERVES_TRAFFIC,
                discovered_by=DISCOVERED_BY_SVC,
                extras={
                    "confidence": "declared_k8s",
                    "semantics": "sync",
                    "selector": selector,
                    "service_type": meta_json.get("service_type"),
                },
            )
            stats["edges_serves_traffic"] += 1

    db.commit()
    logger.info(
        "k8s_topology_resources.services_done fetched=%d nodes=%d edges=%d "
        "skipped_no_selector=%d skipped_no_match=%d skipped_self_loop=%d",
        stats["services_fetched"], stats["nodes_upserted"],
        stats["edges_serves_traffic"],
        stats["skipped_no_selector"], stats["skipped_no_match"],
        stats["skipped_self_loop"],
    )
    return stats


# ── ingress sync ────────────────────────────────────────────────────────────


def sync_all_ingresses_declarative(db: Session) -> Dict[str, int]:
    """Declarative Ingress → routes_to Service edges.

    Параллельный источник к `app.knowledge_graph.k8s_ingress_sync` (который
    делает external-entry-points через `ingress:<host>` synthetic-узлы).
    Здесь нода — сам Ingress-resource `ingress:<name>` per-namespace, и edges
    `routes_to` ведут на backing Services. Это даёт ответ на «какие
    Services торчат наружу через какой ingress».

    Возвращает stats:
        ingresses_fetched / routes_seen / nodes_created / edges_created
        / skipped_no_backend_match
    """
    ingresses = _kubectl_get_all("ingresses")
    stats = {
        "ingresses_fetched": len(ingresses),
        "routes_seen": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "skipped_no_backend_match": 0,
    }

    for ing in ingresses:
        meta = ing.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        ing_name = meta.get("name")
        if not ing_name:
            continue

        routes = _extract_ingress_routes(ing)
        if not routes:
            continue

        ing_node_name = f"ingress:{ing_name}"
        ing_node = upsert_service(
            db,
            namespace=ns,
            name=ing_node_name,
            team_owner="external",
            synthetic=True,
            metadata={
                "k8s_ingress": {
                    "ingress_name": ing_name,
                    "ingress_class": (ing.get("spec") or {}).get("ingressClassName"),
                    "hosts": sorted({h for h, _, _ in routes if h}),
                },
            },
        )
        stats["nodes_created"] += 1

        for host, path, backend_name in routes:
            stats["routes_seen"] += 1
            backend = (
                db.query(Service)
                .filter_by(namespace=ns, name=backend_name)
                .one_or_none()
            )
            if backend is None:
                stats["skipped_no_backend_match"] += 1
                continue
            upsert_edge(
                db,
                src=ing_node,
                dst=backend,
                kind=EDGE_ROUTES_TO,
                discovered_by=DISCOVERED_BY_INGRESS,
                extras={
                    "host": host or "*",
                    "path": path,
                    "confidence": "declared_k8s",
                    "semantics": "sync",
                },
            )
            stats["edges_created"] += 1

    db.commit()
    logger.info(
        "k8s_topology_resources.ingresses_done fetched=%d routes=%d edges=%d "
        "skipped=%d",
        stats["ingresses_fetched"], stats["routes_seen"],
        stats["edges_created"], stats["skipped_no_backend_match"],
    )
    return stats


# ── orchestrator ────────────────────────────────────────────────────────────


def sync_topology_resources(db: Session) -> Dict[str, Any]:
    """Main entry: один tick — fetch deployments один раз, потом services,
    потом ingresses. Возвращает dict с обоими slice-ами stats для observability.
    """
    deployments_index = _index_deployments_by_ns(_kubectl_get_deployments_all())
    svc_stats = sync_all_services(db, deployments_index=deployments_index)
    ing_stats = sync_all_ingresses_declarative(db)
    return {"services": svc_stats, "ingresses": ing_stats}


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = sync_topology_resources(db)
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
    _ = sys.argv  # noqa: SIM107 (placeholder для будущего per-ns CLI)
