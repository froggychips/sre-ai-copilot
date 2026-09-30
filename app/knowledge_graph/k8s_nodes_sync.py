"""Ноды кластера (зона/регион) и точки входа трафика → kg_nodes / kg_entrypoints.

Два факта, которых граф не знал:

* **зона ноды.** На нодах есть метки `topology.kubernetes.io/zone` и
  `topology.kubernetes.io/region`, но ни один синк не звал
  `kubectl get nodes` — нодовый алерт не мог ответить, что ещё стоит в том
  же датацентре;
* **через какую ноду входит host.** LoadBalancer-сервисы — MetalLB L2 с
  `externalTrafficPolicy: Local`: публичный IP балансировщика обычно равен
  адресу самой ноды. Отказ такой ноды роняет вход для всех host-ов, что на неё
  резолвятся, в том числе host-ов чужих стендов, чьих подов на ноде нет.

`sync_nodes` — upsert строки на ноду; нода, пропавшая из API, получает
`deleted_at` (строка остаётся). `sync_entrypoints` — host-ы из Ingress,
DNS-резолв (IPv4), сверка с адресами живых нод и IP LoadBalancer-сервисов.

Деградация как у соседних синков: kubectl не ответил — тик пропускается
целиком, ничего не помечается удалённым (пустой ответ — не «нод нет»).
Host, который не резолвится, остаётся строкой с пустым `resolved_ips`.

CLI:
    python -m app.knowledge_graph.k8s_nodes_sync
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.knowledge_graph.kubectl_breaker import KubectlCircuitOpen, run_kubectl
from app.knowledge_graph.schema import K8sNode, TrafficEntrypoint

log = logging.getLogger(__name__)

_KUBECTL_TIMEOUT_S = 60
# Лист сервисов всего кластера — тысячи объектов (см. комментарий к таймауту
# в k8s_topology_resources_sync): чанками и с тем же запасом по времени.
_KUBECTL_SVC_TIMEOUT_S = 180
_KUBECTL_CHUNK = 500

# DNS: getaddrinfo собственного таймаута не имеет, поэтому резолв идёт в пуле
# потоков с общим дедлайном. Host, не успевший за него, считается
# нерезолвнутым на этом тике.
_DNS_WORKERS = 16
_DNS_DEADLINE_S = 60.0

LABEL_ZONE = "topology.kubernetes.io/zone"
LABEL_REGION = "topology.kubernetes.io/region"
# Устаревшие ключи тех же меток: на старых нодах бывают только они.
_LEGACY_ZONE = "failure-domain.beta.kubernetes.io/zone"
_LEGACY_REGION = "failure-domain.beta.kubernetes.io/region"
_ROLE_PREFIX = "node-role.kubernetes.io/"
# Какие ещё метки ноды сохраняем: окружение и королевство. Сравнение — по
# имени после префикса (`example.io/env` тоже подходит).
_KEPT_LABEL_NAMES = frozenset({"env", "environment", "kingdom"})


class NodesFetchError(RuntimeError):
    """kubectl не ответил (таймаут, rc!=0, битый JSON, открыт брейкер).

    Отдельный тип, чтобы отличать сбой от валидного пустого ответа: по сбою
    ничего не помечается удалённым.
    """


# ── kubectl ──────────────────────────────────────────────────────────────────


def _kubectl_items(args: List[str], *, timeout: float, what: str) -> List[Dict[str, Any]]:
    """`kubectl ... -o json` → items. Любой сбой — `NodesFetchError`."""
    try:
        out = run_kubectl(args, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise NodesFetchError(f"{what}: timeout {timeout}s") from e
    except (OSError, KubectlCircuitOpen) as e:
        raise NodesFetchError(f"{what}: {e}") from e
    if out.returncode != 0:
        raise NodesFetchError(
            f"{what}: rc={out.returncode} {(out.stderr or '').strip()[:200]}")
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as e:
        raise NodesFetchError(f"{what}: bad json: {e}") from e
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise NodesFetchError(f"{what}: нет items в ответе")
    return items


def _kubectl_get_nodes() -> List[Dict[str, Any]]:
    return _kubectl_items(["kubectl", "get", "nodes", "-o", "json"],
                          timeout=_KUBECTL_TIMEOUT_S, what="kubectl get nodes")


def _kubectl_get_ingresses() -> List[Dict[str, Any]]:
    return _kubectl_items(
        ["kubectl", "get", "ingresses", "-A", "-o", "json", f"--chunk-size={_KUBECTL_CHUNK}"],
        timeout=_KUBECTL_TIMEOUT_S, what="kubectl get ingresses")


def _kubectl_get_services() -> List[Dict[str, Any]]:
    return _kubectl_items(
        ["kubectl", "get", "services", "-A", "-o", "json", f"--chunk-size={_KUBECTL_CHUNK}"],
        timeout=_KUBECTL_SVC_TIMEOUT_S, what="kubectl get services")


# ── разбор ноды ──────────────────────────────────────────────────────────────


def _curated_labels(labels: Dict[str, Any]) -> Dict[str, str]:
    """Зона, регион и env/kingdom-метки — остальное служебный шум."""
    out: Dict[str, str] = {}
    for k, v in (labels or {}).items():
        local = k.rsplit("/", 1)[-1].lower()
        if (k in (LABEL_ZONE, LABEL_REGION, _LEGACY_ZONE, _LEGACY_REGION)
                or local in _KEPT_LABEL_NAMES or local.startswith("kingdom")):
            out[k] = str(v)
    return out


def parse_node(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Объект Node из API → поля строки kg_nodes (None — без имени)."""
    meta = item.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return None
    labels = meta.get("labels") or {}
    addresses = [
        {"type": a.get("type"), "address": a.get("address")}
        for a in (item.get("status") or {}).get("addresses") or []
        if isinstance(a, dict) and a.get("address")
    ]

    def _first(kind: str) -> Optional[str]:
        return next((a["address"] for a in addresses if a["type"] == kind), None)

    roles = sorted(k[len(_ROLE_PREFIX):] for k in labels
                   if k.startswith(_ROLE_PREFIX) and k[len(_ROLE_PREFIX):])
    return {
        "name": name,
        "zone": labels.get(LABEL_ZONE) or labels.get(_LEGACY_ZONE),
        "region": labels.get(LABEL_REGION) or labels.get(_LEGACY_REGION),
        "internal_ip": _first("InternalIP"),
        "external_ip": _first("ExternalIP"),
        "addresses": addresses,
        "roles": roles,
        "unschedulable": bool((item.get("spec") or {}).get("unschedulable")),
        "labels_json": _curated_labels(labels),
    }


def node_ips(row: Any) -> Set[str]:
    """Все IP-адреса ноды (InternalIP/ExternalIP и прочие, кроме Hostname)."""
    ips: Set[str] = set()
    for a in row.addresses or []:
        if isinstance(a, dict) and a.get("type") != "Hostname" and a.get("address"):
            ips.add(str(a["address"]))
    for ip in (row.internal_ip, row.external_ip):
        if ip:
            ips.add(ip)
    return ips


# ── sync_nodes ───────────────────────────────────────────────────────────────


def sync_nodes(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """kubectl get nodes → upsert kg_nodes, пропавшим — deleted_at.

    stats: nodes_fetched / upserted / created / marked_deleted / errors, и
    `skipped` (причина) при сбое kubectl — тогда в таблице не меняется ничего.
    """
    now = now or datetime.utcnow()
    stats: Dict[str, Any] = {"nodes_fetched": 0, "upserted": 0, "created": 0,
                             "marked_deleted": 0, "errors": 0}
    try:
        items = _kubectl_get_nodes()
    except NodesFetchError as e:
        log.warning("nodes_sync.fetch_failed err=%s — тик пропущен, удалений нет", e)
        stats["errors"] = 1
        stats["skipped"] = f"kubectl: {e}"[:200]
        return stats

    parsed = [p for p in (parse_node(i) for i in items) if p]
    stats["nodes_fetched"] = len(parsed)
    existing = {r.name: r for r in db.query(K8sNode).all()}
    seen: Set[str] = set()
    for p in parsed:
        seen.add(p["name"])
        row = existing.get(p["name"])
        if row is None:
            row = K8sNode(name=p["name"], first_seen_at=now)
            db.add(row)
            existing[p["name"]] = row
            stats["created"] += 1
        for k, v in p.items():
            setattr(row, k, v)
        row.last_seen_at = now
        # Нода вернулась под тем же именем: снова живая. first_seen_at не
        # трогаем — промежуток отсутствия теряется, это цена одной строки
        # на имя.
        row.deleted_at = None
        stats["upserted"] += 1

    # Пустой успешный ответ — не «нод больше нет»: кластер без нод не бывает,
    # а вот RBAC-обрезанный или странный ответ — бывает.
    if parsed:
        for name, row in existing.items():
            if name not in seen and row.deleted_at is None:
                row.deleted_at = now
                stats["marked_deleted"] += 1
    db.commit()
    log.info("nodes_sync.done fetched=%d created=%d deleted=%d",
             stats["nodes_fetched"], stats["created"], stats["marked_deleted"])
    return stats


# ── sync_entrypoints ─────────────────────────────────────────────────────────


def ingress_hosts(ingresses: Iterable[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """host → классы Ingress-ов, которые его обслуживают. Wildcard и пустые
    host-ы пропускаются: их нельзя резолвить как имя."""
    out: Dict[str, Set[str]] = {}
    for ing in ingresses:
        meta = ing.get("metadata") or {}
        spec = ing.get("spec") or {}
        cls = (spec.get("ingressClassName")
               or (meta.get("annotations") or {}).get("kubernetes.io/ingress.class"))
        for rule in spec.get("rules") or []:
            host = str((rule or {}).get("host") or "").strip().lower().rstrip(".")
            if not host or "*" in host:
                continue
            classes = out.setdefault(host, set())
            if cls:
                classes.add(str(cls))
    return out


def lb_service_ips(services: Iterable[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """IP → "namespace/name" LoadBalancer-сервисов с этим IP в status."""
    out: Dict[str, Set[str]] = {}
    for svc in services:
        if (svc.get("spec") or {}).get("type") != "LoadBalancer":
            continue
        meta = svc.get("metadata") or {}
        ref = f"{meta.get('namespace') or 'default'}/{meta.get('name') or '?'}"
        lb = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        for entry in lb:
            ip = (entry or {}).get("ip")
            if ip:
                out.setdefault(str(ip), set()).add(ref)
    return out


def _resolve_host(host: str) -> List[str]:
    """IPv4-адреса host-а стандартным резолвером. Сбой — пустой список."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return []
    return sorted({str(info[4][0]) for info in infos})


def _resolve_hosts(hosts: Iterable[str]) -> Dict[str, List[str]]:
    """Резолв пачкой с общим дедлайном: зависший резолвер не держит тик."""
    hosts = list(hosts)
    if not hosts:
        return {}
    pool = ThreadPoolExecutor(max_workers=_DNS_WORKERS, thread_name_prefix="kg-dns")
    try:
        futures = {h: pool.submit(_resolve_host, h) for h in hosts}
        wait(list(futures.values()), timeout=_DNS_DEADLINE_S)
        out: Dict[str, List[str]] = {}
        for h, f in futures.items():
            if f.done() and not f.cancelled() and f.exception() is None:
                out[h] = f.result()
            else:
                out[h] = []
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def sync_entrypoints(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Ingress host-ы → kg_entrypoints: резолв, сверка с нодами и LB-сервисами.

    Адреса нод берутся из kg_nodes (живые строки), поэтому вызывать после
    `sync_nodes`. Сбой kubectl (ingress или services) — тик пропущен целиком:
    частичная сверка записала бы «входа через ноду нет» там, где мы просто не
    знаем.
    """
    now = now or datetime.utcnow()
    stats: Dict[str, Any] = {"hosts_seen": 0, "resolved": 0, "unresolved": 0,
                             "with_entry_nodes": 0, "created": 0, "marked_deleted": 0,
                             "errors": 0}
    try:
        ingresses = _kubectl_get_ingresses()
        services = _kubectl_get_services()
    except NodesFetchError as e:
        log.warning("entrypoints_sync.fetch_failed err=%s — тик пропущен, удалений нет", e)
        stats["errors"] = 1
        stats["skipped"] = f"kubectl: {e}"[:200]
        return stats

    hosts = ingress_hosts(ingresses)
    lb_by_ip = lb_service_ips(services)
    node_by_ip: Dict[str, Set[str]] = {}
    for n in db.query(K8sNode).filter(K8sNode.deleted_at.is_(None)).all():
        for ip in node_ips(n):
            node_by_ip.setdefault(ip, set()).add(n.name)

    resolved = _resolve_hosts(sorted(hosts))
    stats["hosts_seen"] = len(hosts)
    existing = {r.host: r for r in db.query(TrafficEntrypoint).all()}
    for host, classes in sorted(hosts.items()):
        ips = resolved.get(host) or []
        entry_nodes = sorted({n for ip in ips for n in node_by_ip.get(ip, ())})
        lb_refs = sorted({s for ip in ips for s in lb_by_ip.get(ip, ())})
        stats["resolved" if ips else "unresolved"] += 1
        if entry_nodes:
            stats["with_entry_nodes"] += 1
        row = existing.get(host)
        if row is None:
            row = TrafficEntrypoint(host=host, first_seen_at=now)
            db.add(row)
            existing[host] = row
            stats["created"] += 1
        row.resolved_ips = ips
        row.entry_nodes = entry_nodes
        row.lb_services = lb_refs
        row.ingress_classes = sorted(classes)
        row.last_seen_at = now
        row.deleted_at = None

    # Как у нод: пустой список Ingress-ов при живом кластере — подозрение, а
    # не факт, что все host-ы исчезли.
    if hosts:
        for host, row in existing.items():
            if host not in hosts and row.deleted_at is None:
                row.deleted_at = now
                stats["marked_deleted"] += 1
    db.commit()
    log.info("entrypoints_sync.done hosts=%d resolved=%d via_nodes=%d deleted=%d",
             stats["hosts_seen"], stats["resolved"], stats["with_entry_nodes"],
             stats["marked_deleted"])
    return stats


def sync_nodes_and_entrypoints(db: Session) -> Dict[str, Any]:
    """Оба синка одним тиком: сначала ноды (их адреса нужны сверке входов).

    Сбой одного не отменяет другой. `skipped` на верхнем уровне — только
    когда не ответил ни один: тогда источник недоступен, а не частичен.
    """
    result: Dict[str, Any] = {}
    for key, fn in (("nodes", sync_nodes), ("entrypoints", sync_entrypoints)):
        try:
            result[key] = fn(db)
        except Exception as e:  # noqa: BLE001 — сбой записи одного не роняет другой
            db.rollback()
            log.warning("nodes_sync.%s_failed err=%s", key, e)
            result[key] = {"errors": 1, "skipped": f"{type(e).__name__}: {e}"[:200]}
    if all(r.get("skipped") for r in result.values()):
        result["skipped"] = "; ".join(str(r["skipped"]) for r in result.values())
    return result


if __name__ == "__main__":
    from app.database import SessionLocal
    _db = SessionLocal()
    try:
        print(sync_nodes_and_entrypoints(_db))
    finally:
        _db.close()
