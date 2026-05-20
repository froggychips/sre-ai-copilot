"""Phase 3-B: sync k8s Ingress resources в kg_service_edges.

Каждый Ingress даёт **external entrypoint** для одного-нескольких internal
services. Это первый источник, который раскрывает «откуда приходит трафик»
вне cluster-internal env-scan.

Модель:
- synthetic-узел `ingress:<host>` (team_owner="external") создаётся
  per host. Может быть в любом ns Ingress'а — мы кладём в его ns.
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
from app.knowledge_graph.schema import Service

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


def sync_all_ingresses(db: Session) -> Dict[str, int]:
    """Sync — главная entry point.

    Возвращает stats:
      ingresses_fetched / routes_seen / nodes_created / edges_created
      / skipped_no_backend_match (backend service не существует в KG yet)
    """
    ingresses = _kubectl_get_ingresses_all()
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
        ing_name = meta.get("name") or "?"

        routes = _extract_routes(ing)
        for r in routes:
            stats["routes_seen"] += 1
            host = r["host"]
            backend_name = r["backend"]

            # Backend должен существовать в KG (kg_topology_sync уже видел его
            # как Deployment). Если нет — пропускаем (избегаем фейк-узлов).
            backend = (
                db.query(Service).filter_by(namespace=ns, name=backend_name).one_or_none()
            )
            if backend is None:
                stats["skipped_no_backend_match"] += 1
                continue

            # synthetic-узел внешнего entrypoint
            external_node_name = f"ingress:{host}"
            ext = upsert_service(
                db,
                namespace=ns,
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
                    "path": r["path"],
                    "confidence": "declared_k8s",  # сильнее чем inferred_env
                    "semantics": "sync",
                },
            )
            stats["edges_created"] += 1

    db.commit()
    log.info(
        "ingress_sync.done ingresses=%d routes=%d edges=%d skipped=%d",
        stats["ingresses_fetched"], stats["routes_seen"],
        stats["edges_created"], stats["skipped_no_backend_match"],
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        print(sync_all_ingresses(db))
    finally:
        db.close()
