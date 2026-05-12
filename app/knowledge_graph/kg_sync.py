"""KG topology sync — строит ServiceEdge графа из k8s deployment env vars.

Сканирует deployment'ы в заданных namespace'ах, ищет env vars с URL-паттернами
вида `http://{service}.{namespace}.svc.cluster.local:{port}` или
`http://{service}:{port}` (same-namespace), создаёт `calls`-рёбра.

Запуск:
    python -m app.knowledge_graph.kg_sync          # все namespace из конфига
    python -m app.knowledge_graph.kg_sync preprod  # только preprod-*

Можно также импортировать и вызвать из кода: sync_topology(db, namespaces).
"""
from __future__ import annotations

import logging
import re
import subprocess
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.knowledge_graph.populator import upsert_service, upsert_edge
from app.knowledge_graph.schema import Service

logger = logging.getLogger(__name__)

# URL-паттерн: http(s)://service-name(.namespace)?(.svc.cluster.local)?(:port)?
_SVC_URL_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)(?:\.([a-z0-9][a-z0-9-]*))?(?:\.svc\.cluster\.local)?(?::\d+)?/?$",
    re.IGNORECASE,
)

# Пропускаем явно внешние/нерелевантные сервисы
_SKIP_SERVICE_NAMES = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0",
})
_SKIP_VALUE_FRAGMENTS = ("azure", "google", "openai", "gpt", "amazonaws", "redis", "postgres", "nats")

# Namespace'ы для сканирования по умолчанию (env-prefix → список)
DEFAULT_SCAN_NAMESPACES = [
    "preprod-kingdom1", "preprod-kingdom2", "preprod-kingdom3", "preprod-shared",
    "preupdate-kingdom1", "preupdate-kingdom2", "preupdate-kingdom3", "preupdate-kingdom5", "preupdate-shared",
    "prod-kingdom1", "prod-kingdom2", "prod-kingdom3", "prod-kingdom4", "prod-kingdom5",
    "prod-lo-legal", "prod-shared",
]


def _kubectl_get_deployments(namespace: str) -> List[Dict[str, Any]]:
    """Вернуть список deployment spec'ов из kubectl."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "deployments", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.debug("kubectl failed ns=%s: %s", namespace, result.stderr[:200])
            return []
        return json.loads(result.stdout).get("items", [])
    except Exception as e:
        logger.warning("kg_sync.kubectl_failed ns=%s: %s", namespace, e)
        return []


def _extract_upstreams(
    deploy: Dict[str, Any],
    own_namespace: str,
) -> List[Tuple[str, str]]:
    """Вернуть (service_name, namespace) из env vars деплоя."""
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    upstreams: set[Tuple[str, str]] = set()
    for e in envs:
        v = str(e.get("value") or "")
        if not v.startswith("http"):
            continue
        if any(frag in v.lower() for frag in _SKIP_VALUE_FRAGMENTS):
            continue
        m = _SVC_URL_RE.match(v)
        if not m:
            continue
        svc_name = m.group(1).lower()
        ns = m.group(2) or own_namespace
        if svc_name in _SKIP_SERVICE_NAMES:
            continue
        # Игнорируем ссылки на себя же
        own_name = deploy.get("metadata", {}).get("name", "")
        if svc_name == own_name and ns == own_namespace:
            continue
        upstreams.add((svc_name, ns))

    return sorted(upstreams)


def sync_namespace(db: Session, namespace: str) -> Dict[str, int]:
    """Синхронизировать топологию одного namespace."""
    stats = {"services": 0, "edges": 0, "skipped": 0}
    deploys = _kubectl_get_deployments(namespace)

    for deploy in deploys:
        name = deploy.get("metadata", {}).get("name", "")
        if not name:
            continue

        src = upsert_service(db, namespace=namespace, name=name)
        stats["services"] += 1

        upstreams = _extract_upstreams(deploy, namespace)
        for up_svc, up_ns in upstreams:
            dst = upsert_service(db, namespace=up_ns, name=up_svc)
            upsert_edge(db, src=src, dst=dst, kind="calls", discovered_by="kg_sync/env_vars")
            stats["edges"] += 1

    return stats


def sync_topology(
    db: Session,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Синхронизировать топологию всех namespace'ов. Коммит — снаружи."""
    namespaces = namespaces or DEFAULT_SCAN_NAMESPACES
    total = {"services": 0, "edges": 0, "namespaces": 0, "errors": 0}

    for ns in namespaces:
        try:
            stats = sync_namespace(db, ns)
            total["services"] += stats["services"]
            total["edges"] += stats["edges"]
            total["namespaces"] += 1
            if stats["services"] > 0:
                logger.info("kg_sync.ns_done ns=%s services=%d edges=%d",
                            ns, stats["services"], stats["edges"])
        except Exception as e:
            logger.warning("kg_sync.ns_failed ns=%s: %s", ns, e)
            total["errors"] += 1

    db.commit()
    logger.info("kg_sync.done total=%s", total)
    return total


if __name__ == "__main__":
    import sys
    from app.database import SessionLocal

    filter_prefix = sys.argv[1] if len(sys.argv) > 1 else None
    namespaces = (
        [ns for ns in DEFAULT_SCAN_NAMESPACES if ns.startswith(filter_prefix)]
        if filter_prefix
        else DEFAULT_SCAN_NAMESPACES
    )

    db = SessionLocal()
    try:
        result = sync_topology(db, namespaces)
        print(f"Done: {result}")
    finally:
        db.close()
