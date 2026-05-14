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

logger = logging.getLogger(__name__)

# URL-паттерн: http(s)://service-name(.namespace)?(.svc.cluster.local)?(:port)?
_SVC_URL_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)(?:\.([a-z0-9][a-z0-9-]*))?(?:\.svc\.cluster\.local)?(?::\d+)?/?$",
    re.IGNORECASE,
)

# NATS env-pattern: SHARED_NATS_CONNECTION, KINGDOM_NATS_CLIENT_CONNECTION,
# NATS_FOR_CLIENT_SERVICE_CREDS, etc. Значения обычно подставляются runtime
# из ConfigMap-from-file и в env-dump пустые — но НАЛИЧИЕ переменной
# уже сигнал зависимости от NATS-cluster. На графе это синтетический
# узел `nats-shared` / `nats-kingdom`, edge_kind=`uses_nats`.
_NATS_ENV_RE = re.compile(
    r"^(?P<scope>SHARED|KINGDOM)_NATS(?:_CLIENT)?_(?:CONNECTION|CREDS)$"
    r"|^NATS_FOR_(?P<purpose>[A-Z_]+)_(?:CREDS|CONNECTION)$",
    re.IGNORECASE,
)

# Env-prefix к namespace для определения "shared"-кластера NATS.
# prod-kingdom1 → prod-shared, preprod-kingdom2 → preprod-shared, etc.
_NAMESPACE_ENV_PREFIX_RE = re.compile(r"^(prod|preprod|preupdate)(?:-|$)")

# Synthetic-сервисы — по дизайну никогда не имеют edges (backup-cron'ы,
# nats-tools, observability-exporters). Раньше засчитывались в Orphan %
# и раздували её до 33%. Теперь помечаются `synthetic=true` и исключаются
# из `kg_quality_section`. Паттерны намеренно узкие.
_SYNTHETIC_EXACT_NAMES = frozenset({
    "nats-box",
    "nats-client-box",
    "nats-exporter-prometheus-nats-exporter",
    "seq",
    "redis-exporter",
})
_SYNTHETIC_SUFFIXES = ("-db-backup", "-cron")


def _is_synthetic_service(name: str) -> bool:
    """True если service по имени попадает под synthetic-паттерн.

    Изолированные cron-backups / NATS-tools / observability-exporters —
    они НЕ ожидают edges, не должны считаться orphan.
    """
    if not name:
        return False
    if name in _SYNTHETIC_EXACT_NAMES:
        return True
    return any(name.endswith(s) for s in _SYNTHETIC_SUFFIXES)

# Пропускаем явно внешние/нерелевантные сервисы.
# Это blacklist для фильтрации значений из env-переменных подов,
# а не bind-адрес сервера — B104 здесь ложно-положительный.
_SKIP_SERVICE_NAMES = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0",  # nosec B104 — blacklist value, not a bind call
})
_SKIP_VALUE_FRAGMENTS = ("azure", "google", "openai", "gpt", "amazonaws", "redis", "postgres", "nats")

# Namespace-list для KG-sync: тянется из settings.KG_SCAN_NAMESPACES
# (env-var `KG_SCAN_NAMESPACES`, comma-separated). Если пусто — sync_topology
# делает auto-discovery всех non-system namespaces через `kubectl get ns`.
# Раньше тут был хардкоженный список — выпилили чтобы не утекали в репо
# конкретные имена клиентской инфры.


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


def _derive_team_owner(namespace: str) -> Optional[str]:
    """team_owner = namespace без env-prefix.

    prod-kingdom1   → "kingdom1"
    preprod-shared  → "shared"
    preupdate-kingdom3 → "kingdom3"
    sre-ai          → None  (не WO-prefix)
    """
    m = re.match(r"^(?:prod|preprod|preupdate)-(.+)$", namespace)
    return m.group(1) if m else None


def _env_prefix(namespace: str) -> Optional[str]:
    m = _NAMESPACE_ENV_PREFIX_RE.match(namespace)
    return m.group(1) if m else None


def _extract_nats_clusters(
    deploy: Dict[str, Any],
    own_namespace: str,
) -> List[Tuple[str, str]]:
    """Вернуть [(cluster_name, cluster_namespace)] из NATS-env-имён.

    SHARED_NATS_*    → synthetic-node `nats-shared`  в `<env>-shared` namespace
    KINGDOM_NATS_*   → synthetic-node `nats-kingdom` в собственном namespace
    NATS_FOR_*_*     → synthetic-node `nats-purpose` в `<env>-shared` (общий)

    Значения env-переменных не нужны — здесь регистрируется только сам факт
    зависимости от NATS-cluster.
    """
    envs: List[Dict[str, Any]] = []
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        envs += c.get("env") or []

    env_prefix = _env_prefix(own_namespace)
    found: set[Tuple[str, str]] = set()
    for e in envs:
        n = str(e.get("name", "") or "")
        m = _NATS_ENV_RE.match(n)
        if not m:
            continue
        if m.group("scope"):
            scope = m.group("scope").lower()
            if scope == "shared" and env_prefix:
                found.add(("nats-shared", f"{env_prefix}-shared"))
            elif scope == "kingdom":
                # kingdom NATS живёт в этом же namespace
                found.add(("nats-kingdom", own_namespace))
        elif m.group("purpose") and env_prefix:
            # NATS_FOR_X_CREDS — general-purpose, относим в env-shared
            found.add(("nats-purpose", f"{env_prefix}-shared"))
    return sorted(found)


def sync_namespace(db: Session, namespace: str) -> Dict[str, int]:
    """Синхронизировать топологию одного namespace."""
    stats = {"services": 0, "edges": 0, "skipped": 0}
    deploys = _kubectl_get_deployments(namespace)

    src_team = _derive_team_owner(namespace)

    for deploy in deploys:
        name = deploy.get("metadata", {}).get("name", "")
        if not name:
            continue

        src = upsert_service(
            db, namespace=namespace, name=name, team_owner=src_team,
            synthetic=_is_synthetic_service(name),
        )
        stats["services"] += 1

        # URL-based edges (existing flow)
        upstreams = _extract_upstreams(deploy, namespace)
        for up_svc, up_ns in upstreams:
            dst = upsert_service(
                db,
                namespace=up_ns,
                name=up_svc,
                team_owner=_derive_team_owner(up_ns),
            )
            upsert_edge(db, src=src, dst=dst, kind="calls", discovered_by="kg_sync/env_vars")
            stats["edges"] += 1

        # NATS-cluster edges (PR — KG enrichment).
        # Synthetic nodes — представляют общий NATS-кластер в `<env>-shared`
        # либо local-kingdom NATS в текущем namespace. team_owner="platform" —
        # явный маркер что это инфра-узел, а не business-service.
        for nats_name, nats_ns in _extract_nats_clusters(deploy, namespace):
            dst = upsert_service(
                db,
                namespace=nats_ns,
                name=nats_name,
                team_owner="platform",
            )
            upsert_edge(
                db, src=src, dst=dst,
                kind="uses_nats",
                discovered_by="kg_sync/nats_env",
            )
            stats["edges"] += 1

    return stats


_SYSTEM_NAMESPACE_PREFIXES = ("kube-", "openshift-", "cattle-")
_SYSTEM_NAMESPACES = frozenset({"default", "monitoring", "ingress-nginx", "cert-manager"})


def _discover_namespaces() -> List[str]:
    """Auto-discovery: `kubectl get ns` минус system-namespaces.

    Используется когда settings.KG_SCAN_NAMESPACES не выставлен.
    System-namespaces (kube-*, monitoring, cert-manager и т.п.) исключаются.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("kg_sync.discover_failed: %s", result.stderr[:200])
            return []
        all_ns = result.stdout.strip().split()
        return [
            ns for ns in all_ns
            if ns not in _SYSTEM_NAMESPACES
            and not any(ns.startswith(p) for p in _SYSTEM_NAMESPACE_PREFIXES)
        ]
    except Exception as e:
        logger.warning("kg_sync.discover_exception: %s", e)
        return []


def sync_topology(
    db: Session,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Синхронизировать топологию всех namespace'ов. Коммит — снаружи.

    Приоритет источников списка namespaces:
      1. Аргумент `namespaces` (override).
      2. settings.KG_SCAN_NAMESPACES (env-var, comma-separated).
      3. Auto-discovery через `kubectl get ns` минус system-namespaces.
    """
    if namespaces is None:
        from app.config import settings
        raw = (getattr(settings, "KG_SCAN_NAMESPACES", "") or "").strip()
        namespaces = [n.strip() for n in raw.split(",") if n.strip()] if raw else _discover_namespaces()
    if not namespaces:
        logger.warning("kg_sync.no_namespaces_to_scan")
        return {"services": 0, "edges": 0, "namespaces": 0, "errors": 0}
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

    # CLI: python -m app.knowledge_graph.kg_sync [prefix-filter]
    # Без аргумента — sync по settings.KG_SCAN_NAMESPACES / auto-discovery.
    # С prefix — фильтрует auto-discovery list по prefix-у.
    filter_prefix = sys.argv[1] if len(sys.argv) > 1 else None
    namespaces: Optional[List[str]] = None
    if filter_prefix:
        discovered = _discover_namespaces()
        namespaces = [ns for ns in discovered if ns.startswith(filter_prefix)]
    # else: sync_topology возьмёт из settings / auto-discovery

    db = SessionLocal()
    try:
        result = sync_topology(db, namespaces)
        print(f"Done: {result}")
    finally:
        db.close()
