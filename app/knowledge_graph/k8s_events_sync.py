"""A4: sync k8s pod-events в kg_pod_events.

Параллельный к AlertManager источник для root-cause: события Warning
типа OOMKilled / FailedScheduling / ImagePullBackOff / FailedMount /
BackOff / Unhealthy теряются если не вылились в Prometheus alert.

Sync периодический (Celery beat task `k8s_pod_events_sync`). Per-namespace
вызов `kubectl get events -n NS --field-selector type=Warning -o json`,
парсинг JSON, фильтр по reason, idempotent upsert через
`populator.record_pod_event` по `event_uid`.

CLI:
    python -m app.knowledge_graph.k8s_events_sync             # все ns из KG
    python -m app.knowledge_graph.k8s_events_sync prod-shared # один ns
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.populator import record_pod_event
from app.knowledge_graph.schema import Service

logger = logging.getLogger(__name__)

# Diagnostic reasons мы хотим иметь в KG. Информационный шум (Pulled, Created,
# Scheduled, Started, Killing) — пропускаем: в эмбеддах копилоту они бесполезны.
_WARN_REASONS = frozenset({
    "OOMKilled",
    "FailedScheduling",
    "FailedMount",
    "FailedAttachVolume",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "BackOff",                # CrashLoopBackOff и retry-родственники
    "CrashLoopBackOff",
    "Evicted",
    "Preempted",
    "Unhealthy",              # liveness/readiness fail
    "ProbeError",
    "NodeNotReady",
    "NodeNotSchedulable",
    "FailedCreatePodSandBox",
    "FailedKillPod",
    "FailedSync",
})

# Pod-name → deployment-name. ReplicaSet-у k8s даёт hash-suffix 8-10 chars,
# дальше pod-hash 5 chars. Strict: 2 final dash-сегмента из [a-z0-9].
_POD_NAME_DEPLOYMENT_RE = re.compile(
    r"^(?P<dep>.+)-(?P<rs>[a-z0-9]{8,10})-(?P<pod>[a-z0-9]{4,8})$"
)


def _kubectl_get_events_warning(namespace: str) -> List[Dict[str, Any]]:
    """`kubectl get events -n NS --field-selector type=Warning -o json`."""
    try:
        out = subprocess.run(
            [
                "kubectl", "get", "events", "-n", namespace,
                "--field-selector", "type=Warning",
                "-o", "json",
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("k8s_events.timeout namespace=%s", namespace)
        return []
    if out.returncode != 0:
        logger.warning(
            "k8s_events.kubectl_failed namespace=%s rc=%d stderr=%s",
            namespace, out.returncode, out.stderr.strip()[:200],
        )
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        logger.warning("k8s_events.json_decode_failed namespace=%s err=%s", namespace, e)
        return []
    return data.get("items") or []


def _deployment_from_pod_name(pod_name: str) -> Optional[str]:
    """`bot-service-5476d85d74-f626c` → `bot-service`.

    None если pod-name не соответствует стандартному pattern Deployment
    (например, StatefulSet даёт `pg-cluster-0` — не наш кейс для A4).
    """
    if not pod_name:
        return None
    m = _POD_NAME_DEPLOYMENT_RE.match(pod_name)
    return m.group("dep") if m else None


def _parse_k8s_timestamp(ts: Optional[str]) -> Optional[datetime]:
    """k8s ISO format: `2026-05-16T07:30:03Z` → naive datetime в UTC."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def sync_namespace_events(
    db: Session,
    namespace: str,
) -> Dict[str, int]:
    """Один pass: `kubectl get events -n NS` → upsert в kg_pod_events.

    Возвращает {"fetched": N, "added": M, "skipped": K, "errors": E}.
    """
    stats = {"fetched": 0, "added": 0, "skipped": 0, "errors": 0}
    raw_events = _kubectl_get_events_warning(namespace)
    stats["fetched"] = len(raw_events)

    if not raw_events:
        return stats

    # Pre-load deployment-name → service_id map для этого ns (одним запросом
    # вместо N лукапов).
    svc_map: Dict[str, Service] = {
        s.name: s for s in db.query(Service).filter_by(namespace=namespace).all()
    }

    for ev in raw_events:
        try:
            reason = ev.get("reason")
            if not reason or reason not in _WARN_REASONS:
                stats["skipped"] += 1
                continue

            uid = (ev.get("metadata") or {}).get("uid")
            if not uid:
                stats["skipped"] += 1
                continue

            involved = ev.get("involvedObject") or {}
            kind = involved.get("kind")
            obj_name = involved.get("name") or ""
            # Берём только Pod-уровневые события (kind=Pod). События уровня
            # Node / Deployment / PersistentVolume оставляем для отдельных
            # таблиц в будущем.
            if kind != "Pod":
                stats["skipped"] += 1
                continue

            dep_name = _deployment_from_pod_name(obj_name)
            svc = svc_map.get(dep_name) if dep_name else None

            first_seen = (
                _parse_k8s_timestamp(ev.get("firstTimestamp"))
                or _parse_k8s_timestamp(ev.get("eventTime"))
                or _parse_k8s_timestamp((ev.get("metadata") or {}).get("creationTimestamp"))
            )
            if first_seen is None:
                stats["skipped"] += 1
                continue
            last_seen = _parse_k8s_timestamp(ev.get("lastTimestamp")) or first_seen
            count = ev.get("count")

            record_pod_event(
                db,
                service=svc,
                namespace=namespace,
                pod_name=obj_name,
                reason=reason,
                event_uid=uid,
                first_seen=first_seen,
                last_seen=last_seen,
                count=count if isinstance(count, int) else None,
                message=(ev.get("message") or "")[:500],
                type_=ev.get("type"),
                extras={
                    "field_path": involved.get("fieldPath"),
                    "source_component": (ev.get("source") or {}).get("component"),
                    "source_host": (ev.get("source") or {}).get("host"),
                },
            )
            stats["added"] += 1
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "k8s_events.record_failed ns=%s reason=%s err=%s",
                namespace, ev.get("reason"), e,
            )

    db.commit()
    return stats


def sync_all_events(
    db: Session,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Sync events во всех ns из KG (или явно переданных).

    `KG_SCAN_NAMESPACES` env — comma-separated whitelist. Пусто → берём
    из `kg_services.namespace` (distinct).
    """
    if namespaces is None:
        configured = (settings.KG_SCAN_NAMESPACES or "").strip()
        if configured:
            namespaces = [s.strip() for s in configured.split(",") if s.strip()]
        else:
            namespaces = sorted(
                {ns for (ns,) in db.query(Service.namespace).distinct().all()}
            )

    total = {"namespaces": 0, "fetched": 0, "added": 0, "skipped": 0, "errors": 0}
    for ns in namespaces:
        s = sync_namespace_events(db, ns)
        total["namespaces"] += 1
        for k in ("fetched", "added", "skipped", "errors"):
            total[k] += s[k]
    logger.info(
        "k8s_events.sync_done namespaces=%d fetched=%d added=%d skipped=%d errors=%d",
        total["namespaces"], total["fetched"], total["added"],
        total["skipped"], total["errors"],
    )
    return total


if __name__ == "__main__":
    from app.database import SessionLocal

    ns_filter = sys.argv[1:] if len(sys.argv) > 1 else None
    db = SessionLocal()
    try:
        print(sync_all_events(db, namespaces=ns_filter))
    finally:
        db.close()
