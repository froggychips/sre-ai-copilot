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
from app.knowledge_graph.nats_subjects_sync import NATS_SUBJECTS_NAMESPACE
from app.knowledge_graph.populator import record_pod_event
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service

logger = logging.getLogger(__name__)

# Namespace, которых в k8s не существует по определению — они чисто
# KG-конструкция (`nats-subjects` держит subject-узлы из nats_subjects_sync).
# kubectl на них всегда даёт NotFound.
_SYNTHETIC_NAMESPACES = frozenset({NATS_SUBJECTS_NAMESPACE})

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

# StatefulSet pod-name pattern: `<sts>-<ordinal>` (например, `nats-0`,
# `town-db-postgresql-0`). Используется как fallback когда Deployment-regex
# не сматчил (см. _resolve_service_for_pod).
_POD_NAME_STS_RE = re.compile(r"^(?P<sts>.+)-(?P<ord>\d+)$")

# Bitnami init-container создаёт временный pod `<sts>-tmp` (volumePermissions).
# Snimaем суффикс перед матчингом.
_BITNAMI_TMP_SUFFIX = "-tmp"


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
    (например, StatefulSet даёт `pg-cluster-0` — для StS см.
    `_sts_base_candidates`).
    """
    if not pod_name:
        return None
    m = _POD_NAME_DEPLOYMENT_RE.match(pod_name)
    return m.group("dep") if m else None


def _sts_base_candidates(pod_name: str) -> List[str]:
    """Кандидаты на имя сервиса для StatefulSet-пода.

    Порядок приоритета (от точного к широкому):
      1. Полное pod_name (на случай pod-без-ординала, например job-pod).
      2. Strip Bitnami `-tmp` суффикса (init-container volumePermissions).
      3. Strip trailing `-<digits>` (StatefulSet ординал).
      4. Strip и `-tmp`, и ординал.
      5. Strip полу-name: `<base>-postgresql-0` → `<base>` (для случая
         когда kg_services хранит логический сервис типа `town-db`,
         а pod зовут `town-db-postgresql-0`). Только если после strip
         ординала имя оканчивается на хорошо известный bitnami-чарт.

    Возвращает список кандидатов (с дедупликацией), сохраняя порядок
    приоритета. Caller матчит по `svc_map` и берёт первое попадание.
    """
    if not pod_name:
        return []
    candidates: List[str] = []

    def _push(name: str) -> None:
        if name and name not in candidates:
            candidates.append(name)

    _push(pod_name)

    stripped_tmp = pod_name
    if stripped_tmp.endswith(_BITNAMI_TMP_SUFFIX):
        stripped_tmp = stripped_tmp[: -len(_BITNAMI_TMP_SUFFIX)]
        _push(stripped_tmp)

    m = _POD_NAME_STS_RE.match(stripped_tmp)
    if m:
        sts_base = m.group("sts")
        _push(sts_base)
        # Полу-strip: `town-db-postgresql` → `town-db`. Ограничиваем известными
        # bitnami/инфра чартами, чтобы не калечить имена типа `wo-api-shared`.
        for chart in ("-postgresql", "-clickhouse", "-redis", "-mongodb", "-mariadb"):
            if sts_base.endswith(chart):
                _push(sts_base[: -len(chart)])
                break

    return candidates


def _kubectl_get_pods_owner_map(namespace: str) -> Dict[str, str]:
    """`kubectl get pods -n NS -o json` → {pod_name: owner_name}.

    Owner = первый ownerReference (StatefulSet / ReplicaSet / DaemonSet / Job).
    Для ReplicaSet возвращаем имя ReplicaSet — caller затем сматчит его как
    Deployment через `_POD_NAME_DEPLOYMENT_RE` или через strip hash-суффикса.

    Используется как поздний fallback, когда ни Deployment-regex, ни
    StatefulSet-suffix-strip не нашли service. Один kubectl call на ns.
    Пустой dict при ошибке — не валим sync.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("k8s_events.pods_timeout namespace=%s", namespace)
        return {}
    if out.returncode != 0:
        logger.warning(
            "k8s_events.pods_kubectl_failed namespace=%s rc=%d stderr=%s",
            namespace, out.returncode, out.stderr.strip()[:200],
        )
        return {}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        logger.warning("k8s_events.pods_json_decode_failed namespace=%s err=%s", namespace, e)
        return {}

    result: Dict[str, str] = {}
    for pod in data.get("items") or []:
        md = pod.get("metadata") or {}
        name = md.get("name")
        owners = md.get("ownerReferences") or []
        if not name or not owners:
            continue
        # Первый owner — основной (k8s гарантирует controller=true только один).
        owner = owners[0]
        owner_name = owner.get("name")
        owner_kind = owner.get("kind")
        if not owner_name:
            continue
        # ReplicaSet → strip hash-суффикс до Deployment-имени.
        if owner_kind == "ReplicaSet":
            # `bot-service-5476d85d74` → `bot-service`.
            m = re.match(r"^(?P<dep>.+)-[a-z0-9]{8,10}$", owner_name)
            if m:
                result[name] = m.group("dep")
                continue
        result[name] = owner_name
    return result


def _resolve_service_for_pod(
    pod_name: str,
    svc_map: Dict[str, "Service"],
    owner_map_loader,
) -> tuple:
    """pod_name → (service|None, resolver_tag).

    Порядок:
      1. `_deployment_from_pod_name` (старый strict regex) — для Deployment-подов.
      2. `_sts_base_candidates` — для StatefulSet'ов и Bitnami `-tmp` init-подов.
      3. `owner_map_loader()` — lazy kubectl batch lookup ownerReferences.

    `owner_map_loader` — callable () → Dict[pod_name, owner_name], кэшируется
    на уровне sync_namespace_events (один вызов на ns).
    """
    if not pod_name:
        return None, "none"

    # 1. Deployment hash strip (старое поведение, не ломаем).
    dep_name = _deployment_from_pod_name(pod_name)
    if dep_name:
        svc = svc_map.get(dep_name)
        if svc is not None:
            return svc, "pod_hash"

    # 2. StatefulSet / Bitnami-tmp fallback.
    for cand in _sts_base_candidates(pod_name):
        svc = svc_map.get(cand)
        if svc is not None:
            if cand == pod_name:
                tag = "pod_name_exact"
            elif pod_name.endswith(_BITNAMI_TMP_SUFFIX) and cand == pod_name[: -len(_BITNAMI_TMP_SUFFIX)]:
                tag = "sts_suffix_strip_tmp"
            else:
                tag = "sts_suffix_strip"
            return svc, tag

    # 3. Owner-ref lookup (lazy, один kubectl call на ns).
    owner_map = owner_map_loader()
    owner_name = owner_map.get(pod_name)
    if owner_name:
        svc = svc_map.get(owner_name)
        if svc is not None:
            return svc, "owner_ref"
        # Owner может быть, например, `town-db-postgresql` (StS) —
        # пробуем те же strip-кандидаты на owner_name.
        for cand in _sts_base_candidates(owner_name):
            svc = svc_map.get(cand)
            if svc is not None:
                return svc, "owner_ref_strip"

    return None, "none"


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
    # node_kind='service': в kg_services теперь два типа узлов, и без фильтра
    # одноимённый workload-узел перетирал бы Service в этом dict-comprehension
    # (какой победит — зависело бы от порядка строк).
    svc_map: Dict[str, Service] = {
        s.name: s
        for s in db.query(Service).filter_by(
            namespace=namespace, node_kind=NODE_KIND_SERVICE,
        ).all()
    }

    # Lazy owner-ref map: дёргаем `kubectl get pods` только если standard +
    # sts-suffix fallback не нашли service хоть для одного pod'а. Кэшируется
    # внутри замыкания, чтобы не делать N вызовов на ns.
    _owner_map_cache: Dict[str, Dict[str, str]] = {}

    def _owner_map_loader() -> Dict[str, str]:
        if "v" not in _owner_map_cache:
            _owner_map_cache["v"] = _kubectl_get_pods_owner_map(namespace)
        return _owner_map_cache["v"]

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

            svc, resolver_tag = _resolve_service_for_pod(
                obj_name, svc_map, _owner_map_loader,
            )

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

            # SAVEPOINT на событие: при IntegrityError/DataError на одном
            # event Session не уходит в aborted-состояние, и финальный
            # db.commit() этого ns не падает с PendingRollbackError, теряя
            # все успешно записанные ранее события.
            with db.begin_nested():
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
                        "resolver": resolver_tag,
                    },
                )
            stats["added"] += 1
        except Exception as e:
            # begin_nested() уже откатил SAVEPOINT битого event; Session чиста.
            stats["errors"] += 1
            logger.warning(
                "k8s_events.record_failed ns=%s reason=%s err=%s",
                namespace, ev.get("reason"), e,
            )

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stats


def _scannable_kg_namespaces(db: Session) -> List[str]:
    """Namespace из `kg_services`, которые имеет смысл спрашивать у kubectl.

    Отсекаем те, которых в кластере нет:
      * синтетические (`_SYNTHETIC_NAMESPACES`) — `nats-subjects` живёт только
        в KG;
      * мёртвые — ns, где не осталось ни одного живого (`synthetic=False`)
        узла. Именно так выглядит namespace после `drift_cleanup`: он
        помечает `synthetic=True` всем сервисам ns, исчезнувшего из кластера.

    Раньше перебирались ВСЕ distinct namespace: на каждый мёртвый/синтетический
    уходило до двух kubectl-вызовов (events + pods) и warning каждые 10 минут
    (beat-интервал events-синка). Живые ns не страдают: `ingress:<host>` и
    прочие synthetic-узлы стоят рядом с реальными сервисами, так что ns
    остаётся в выборке.
    """
    rows = (
        db.query(Service.namespace)
        .filter(Service.synthetic.is_(False))
        .distinct()
        .all()
    )
    return sorted(
        {ns for (ns,) in rows if ns and ns not in _SYNTHETIC_NAMESPACES}
    )


def sync_all_events(
    db: Session,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Sync events во всех ns из KG (или явно переданных).

    `KG_SCAN_NAMESPACES` env — comma-separated whitelist. Пусто → берём
    живые ns из `kg_services.namespace` (см. `_scannable_kg_namespaces`).
    """
    if namespaces is None:
        configured = (settings.KG_SCAN_NAMESPACES or "").strip()
        if configured:
            namespaces = [s.strip() for s in configured.split(",") if s.strip()]
        else:
            namespaces = _scannable_kg_namespaces(db)

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
