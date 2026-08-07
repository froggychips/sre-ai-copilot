"""Phase 3-B: sync k8s Ingress resources в kg_service_edges.

Каждый Ingress даёт **external entrypoint** для одного-нескольких internal
services. Это первый источник, который раскрывает «откуда приходит трафик»
вне cluster-internal env-scan.

Модель:
- synthetic-узел `ingress:<host>` (team_owner="external") — ОДИН на
  hostname, независимо от namespace. Если узел с этим именем уже есть
  где-то в KG — переиспользуем канонический (лексикографически минимальный
  ns, детерминизм как у db:-узлов в kg_sync); иначе создаём в ns Ingress-а.
  Раньше узел плодился per-namespace: один host в N ns давал N узлов,
  деливших один external_probe:{host}-fingerprint.
- Edge `ingress:<host>` → `<backend_svc>` (тот же ns у Ingress), kind=`calls`,
  discovered_by=`kg_sync/ingress`.

Result в embed:
- `upstream_of(auth-service)` начинает возвращать `ingress:auth.lastoasisgame.com`
- В secции «Inbound callers» в #error embed: 1 через `calls` (external)
- В blast radius / why-matters эвристиках узел будет учтён.

CLI:
    python -m app.knowledge_graph.k8s_ingress_sync             # все ns
    python -m app.knowledge_graph.k8s_ingress_sync preprod-kingdom1
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service

log = logging.getLogger(__name__)


def _kubectl_get_ingresses_all() -> List[Dict[str, Any]]:
    """kubectl get ingresses -A -o json → list."""
    out = subprocess.run(
        ["kubectl", "get", "ingresses", "-A", "-o", "json"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if out.returncode != 0:
        log.warning(
            "ingress_sync.kubectl_failed rc=%d stderr=%s",
            out.returncode, out.stderr.strip()[:200],
        )
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        log.warning("ingress_sync.json_decode_failed %s", e)
        return []
    return data.get("items") or []


def _extract_routes(ing: Dict[str, Any]) -> List[Dict[str, str]]:
    """Из Ingress spec.rules вытащить (host, backend_svc, path).

    Поддерживаем networking.k8s.io/v1 form (`backend.service.name`).
    `defaultBackend` тоже учитываем (без host — `*` ловит весь трафик).
    """
    out: List[Dict[str, str]] = []
    spec = ing.get("spec") or {}

    # defaultBackend (если ловит unmatched)
    db = spec.get("defaultBackend") or {}
    db_svc = (db.get("service") or {}).get("name")
    if db_svc:
        out.append({"host": "*", "backend": db_svc, "path": "/*"})

    for rule in spec.get("rules") or []:
        host = rule.get("host") or "*"
        http = rule.get("http") or {}
        for path in http.get("paths") or []:
            backend = path.get("backend") or {}
            svc = (backend.get("service") or {}).get("name")
            if svc:
                out.append({
                    "host": host,
                    "backend": svc,
                    "path": path.get("path") or "/",
                })
    return out


def _canonical_host_node_ns(db: Session, host: str, fallback_ns: str) -> str:
    """Namespace для `ingress:<host>`-узла: host — глобальное имя, узел один.

    Если узел(ы) с этим именем уже есть — берём лексикографически
    минимальный namespace (детерминизм; тот же приём, что у db:-узлов в
    `kg_sync._known_db_node_namespaces`). Иначе — ns текущего Ingress-а.
    Так один host в N namespace-ах перестаёт плодить N узлов, деливших
    единственный `external_probe:{host}`-fingerprint.
    """
    rows = (
        db.query(Service.namespace)
        .filter(Service.name == f"ingress:{host}")
        .all()
    )
    if rows:
        return min(ns for (ns,) in rows)
    return fallback_ns


def _sync_one_route(
    db: Session,
    *,
    ns: str,
    ing_name: str,
    route: Dict[str, str],
    stats: Dict[str, int],
) -> None:
    """Обработать один route. Вызывается под per-item SAVEPOINT-ом."""
    host = route["host"]
    backend_name = route["backend"]

    # Backend должен существовать в KG (kg_topology_sync уже видел его
    # как Deployment). Если нет — пропускаем (избегаем фейк-узлов).
    backend = (
        db.query(Service)
        .filter_by(namespace=ns, name=backend_name, node_kind=NODE_KIND_SERVICE)
        .one_or_none()
    )
    if backend is None:
        stats["skipped_no_backend_match"] += 1
        return

    # synthetic-узел внешнего entrypoint — один на hostname (см.
    # _canonical_host_node_ns), не per-namespace.
    external_node_name = f"ingress:{host}"
    node_ns = _canonical_host_node_ns(db, host, ns)
    ext = upsert_service(
        db,
        namespace=node_ns,
        name=external_node_name,
        team_owner="external",
        synthetic=True,
    )
    stats["nodes_created"] += 1  # idempotent: upsert не дубль; считаем "touched"

    upsert_edge(
        db, src=ext, dst=backend, kind="calls",
        discovered_by="kg_sync/ingress",
        extras={
            "ingress_name": ing_name,
            "path": route["path"],
            "confidence": "declared_k8s",  # сильнее чем inferred_env
            "semantics": "sync",
        },
    )
    stats["edges_created"] += 1


def sync_all_ingresses(db: Session) -> Dict[str, int]:
    """Sync — главная entry point.

    Возвращает stats:
      ingresses_fetched / routes_seen / nodes_created / edges_created
      / skipped_no_backend_match (backend service не существует в KG yet)
      / errors (routes, откатившиеся per-item savepoint-ом)
    """
    ingresses = _kubectl_get_ingresses_all()
    stats = {
        "ingresses_fetched": len(ingresses),
        "routes_seen": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "skipped_no_backend_match": 0,
        "errors": 0,
    }

    for ing in ingresses:
        meta = ing.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        ing_name = meta.get("name") or "?"

        routes = _extract_routes(ing)
        for r in routes:
            stats["routes_seen"] += 1
            try:
                # SAVEPOINT на route: один DataError не переводит Session в
                # aborted-состояние и не роняет весь tick (зеркалит
                # per-item SAVEPOINT из k8s_events_sync).
                with db.begin_nested():
                    _sync_one_route(
                        db, ns=ns, ing_name=ing_name, route=r, stats=stats,
                    )
            except Exception as e:
                stats["errors"] += 1
                log.warning(
                    "ingress_sync.route_failed ns=%s ingress=%s host=%s err=%s",
                    ns, ing_name, r.get("host"), e,
                )

    db.commit()
    log.info(
        "ingress_sync.done ingresses=%d routes=%d edges=%d skipped=%d errors=%d",
        stats["ingresses_fetched"], stats["routes_seen"],
        stats["edges_created"], stats["skipped_no_backend_match"],
        stats["errors"],
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        print(sync_all_ingresses(db))
    finally:
        db.close()
