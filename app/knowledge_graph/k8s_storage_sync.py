"""KG Coverage #2: PVC / PV / storage signals → knowledge graph.

Самый ценный source/cost в KG на сегодня: ClickHouse / Postgres «упал»
= в 95% случаев диск кончился. До этого модуля KG не имел никакого
storage-слоя.

Что сюда попадает:

* **PVC** (`PersistentVolumeClaim`, core/v1, namespace-scoped) →
  `kg_storage_volumes` (kind='pvc'). Атрибуты: storage_class, access_modes,
  phase (Bound/Pending/Lost), capacity_bytes из spec.resources.requests.storage,
  volume_name (имя bound PV).

* **PV** (`PersistentVolume`, core/v1, cluster-scoped) → `kg_storage_volumes`
  (kind='pv', namespace=''). Атрибуты: storage_class, access_modes, phase
  (Bound/Pending/Released/Available/Failed), capacity_bytes из spec.capacity.storage.

* **Edges** в `kg_volume_edges`:
    - `uses_volume`: Service (kg_services) → PVC. Источник: scan всех pod'ов
      cluster-wide, для каждого `pod.spec.volumes[].persistentVolumeClaim.claimName`
      создаётся edge от owning Service (через ownerReference Deployment/StatefulSet/
      ReplicaSet) к PVC.
    - `bound_to`: PVC → PV через `pvc.spec.volumeName`.

* **disk_pct enrichment** (опционально, флаг STORAGE_METRICS_ENABLED):
  PromQL `100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes`
  per (namespace, persistentvolumeclaim). Если scrape config kubelet_volume_stats_*
  не настроен — все ответы = 0, отличить от «реально не использован» нельзя,
  поэтому Wave 1 default OFF.

Beat task `kg_storage_sync` каждые 30 минут — storage редко меняется
(claim создаётся ~раз в неделю, capacity статична), но мы хотим ловить
phase-переходы Bound→Released в течение получаса.

CLI: `python -m app.knowledge_graph.k8s_storage_sync`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service, StorageVolume, VolumeEdge

logger = logging.getLogger(__name__)

_KUBECTL_TIMEOUT_S = 30

# Edge kinds — источник истины `app.knowledge_graph.contract.EDGE_KINDS`.
# Локальные алиасы — лишь сахар для read-сайта, чтобы при grep'е по
# модулю их сразу видеть.
EDGE_USES_VOLUME = "uses_volume"  # Service → PVC (kg_volume_edges)
EDGE_BOUND_TO = "bound_to"        # PVC → PV (kg_volume_edges)
# NB: при добавлении нового kind — сначала добавить в contract.EDGE_KINDS
# с status='planned', потом писать sync; только после merge переключать
# на 'active' и бампать KG_SCHEMA_VERSION.

# Node kinds (для VolumeEdge.src_kind / dst_kind)
NODE_SERVICE = "service"
NODE_PVC = "pvc"
NODE_PV = "pv"

DISCOVERED_BY_PODS = "k8s_storage/pod_volumes"
DISCOVERED_BY_PVC_SPEC = "k8s_storage/pvc_spec"


# ── kubectl wrappers ────────────────────────────────────────────────────────


def _kubectl_get_all(resource: str) -> List[Dict[str, Any]]:
    """`kubectl get <resource> -A -o json` → items list (или [] при failure).

    Не raise: failure не должна валить beat-loop. Логируем warning со
    stderr-фрагментом и идём дальше — следующий tick подхватит.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", resource, "-A", "-o", "json"],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning("k8s_storage.kubectl_timeout resource=%s", resource)
        return []
    except Exception as e:
        logger.warning(
            "k8s_storage.kubectl_exception resource=%s err=%s", resource, e,
        )
        return []

    if out.returncode != 0:
        logger.warning(
            "k8s_storage.kubectl_failed resource=%s rc=%d stderr=%s",
            resource, out.returncode, (out.stderr or "").strip()[:200],
        )
        return []

    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as e:
        logger.warning(
            "k8s_storage.json_decode_failed resource=%s err=%s", resource, e,
        )
        return []

    items = data.get("items") or []
    return items if isinstance(items, list) else []


# ── pure helpers ────────────────────────────────────────────────────────────

# k8s quantity → bytes. Покрываем только формат, который реально приходит:
# "100Gi", "500Mi", "2Ti", "10G", "100M", голое число (bytes).
# Полный CR-grammar https://github.com/kubernetes/apimachinery/.../resource/quantity.go
# намеренно НЕ реализуем (там +десятичные степени, +e-notation) — переусложнение.
_QUANTITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]i?)?\s*$")
_QUANTITY_UNITS = {
    "":   1,
    "K":  1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
}


def _parse_capacity_to_bytes(qty: Optional[str]) -> Optional[int]:
    """`"100Gi"` → 107374182400 bytes. None при unparseable.

    Намеренно НЕ raise — capacity-parsing не должен валить sync. Логируем
    warning один раз на unknown form (debug-level чтобы не шуметь).
    """
    if not qty:
        return None
    if isinstance(qty, (int, float)):
        return int(qty)
    m = _QUANTITY_RE.match(str(qty))
    if not m:
        logger.debug("k8s_storage.unparseable_quantity qty=%r", qty)
        return None
    value, unit = m.group(1), m.group(2) or ""
    mult = _QUANTITY_UNITS.get(unit)
    if mult is None:
        return None
    try:
        return int(float(value) * mult)
    except (TypeError, ValueError):
        return None


def _extract_pvc_fields(pvc: Dict[str, Any]) -> Dict[str, Any]:
    """PVC JSON → dict нужный для upsert StorageVolume(kind='pvc')."""
    meta = pvc.get("metadata") or {}
    spec = pvc.get("spec") or {}
    status = pvc.get("status") or {}
    requests = (spec.get("resources") or {}).get("requests") or {}
    capacity_str = requests.get("storage")  # nosec B113 — bandit false positive: requests это dict, не httpx
    # При Bound — реальный capacity берётся из status.capacity (= размер PV).
    # Это полезно, когда PVC запросил 100Gi, а dynamic provisioner выдал 110Gi.
    if status.get("capacity") and status["capacity"].get("storage"):
        capacity_str = status["capacity"]["storage"]
    return {
        "kind": "pvc",
        "namespace": meta.get("namespace") or "default",
        "name": meta.get("name") or "",
        "capacity_bytes": _parse_capacity_to_bytes(capacity_str),
        "storage_class": spec.get("storageClassName"),
        "phase": status.get("phase"),
        "access_modes": spec.get("accessModes") or [],
        "volume_name": spec.get("volumeName"),
        "metadata_json": {
            "labels": (meta.get("labels") or {}),
            "annotations": (meta.get("annotations") or {}),
            "volume_mode": spec.get("volumeMode"),
        },
    }


def _extract_pv_fields(pv: Dict[str, Any]) -> Dict[str, Any]:
    """PV JSON → dict для upsert StorageVolume(kind='pv', namespace='')."""
    meta = pv.get("metadata") or {}
    spec = pv.get("spec") or {}
    status = pv.get("status") or {}
    capacity_str = (spec.get("capacity") or {}).get("storage")
    claim_ref = spec.get("claimRef") or {}
    # CSI / hostPath / local — кто провизионер. Хелпит дебагнуть `Released`
    # тома, у которых provisioner = local-path-provisioner: дёшево восстанавливаются.
    source_type = None
    for k in ("csi", "hostPath", "local", "nfs", "iscsi"):
        if spec.get(k):
            source_type = k
            break
    return {
        "kind": "pv",
        "namespace": "",
        "name": meta.get("name") or "",
        "capacity_bytes": _parse_capacity_to_bytes(capacity_str),
        "storage_class": spec.get("storageClassName"),
        "phase": status.get("phase"),
        "access_modes": spec.get("accessModes") or [],
        # У PV `volume_name` не имеет смысла, но колонка nullable=True —
        # просто оставляем None.
        "volume_name": None,
        "metadata_json": {
            "labels": (meta.get("labels") or {}),
            "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
            "source_type": source_type,
            "claim_ref": (
                f"{claim_ref.get('namespace')}/{claim_ref.get('name')}"
                if claim_ref.get("name") else None
            ),
        },
    }


def _pod_owner_chain_to_deployment(
    pod: Dict[str, Any],
    rs_owner_index: Dict[Tuple[str, str], str],
) -> Optional[Tuple[str, str]]:
    """Из pod-а через ownerReferences вытащить (namespace, deployment_name).

    k8s chain: Pod → ReplicaSet → Deployment (для Deployment'ов) или
    Pod → StatefulSet/DaemonSet/Job напрямую (там controller сам owns Pod).

    `rs_owner_index` — pre-built map (ns, rs_name) → deployment_name —
    чтобы один раз сделать `kubectl get rs -A` и потом O(1) lookup.

    Возвращает None если owner-chain не приводит к рабочему workload
    (например, standalone Pod без ownerRefs — kubectl run, debug pods).
    """
    meta = pod.get("metadata") or {}
    ns = meta.get("namespace") or "default"
    owner_refs = meta.get("ownerReferences") or []
    for ref in owner_refs:
        if not ref.get("controller"):
            continue
        kind = ref.get("kind")
        name = ref.get("name")
        if not name:
            continue
        if kind in ("StatefulSet", "DaemonSet", "Job"):
            # Эти controllers owns Pod напрямую — name = workload name.
            return (ns, name)
        if kind == "ReplicaSet":
            dep_name = rs_owner_index.get((ns, name))
            if dep_name:
                return (ns, dep_name)
            # ReplicaSet без owning Deployment — orphan (manual scale-down etc.).
            # Возвращаем сам RS-name — это всё равно «уникальный workload identifier».
            return (ns, name)
    return None


def _build_rs_to_deployment_index(
    replicasets: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], str]:
    """Pre-built (ns, rs_name) → deployment_name map. Один проход по RS."""
    idx: Dict[Tuple[str, str], str] = {}
    for rs in replicasets:
        meta = rs.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        rs_name = meta.get("name")
        if not rs_name:
            continue
        for ref in (meta.get("ownerReferences") or []):
            if ref.get("controller") and ref.get("kind") == "Deployment":
                idx[(ns, rs_name)] = ref.get("name") or ""
                break
    return idx


def _pod_pvc_claims(pod: Dict[str, Any]) -> List[str]:
    """Список PVC.claimName из pod.spec.volumes (только те что persistentVolumeClaim).

    Дедуп: один PVC может встретиться несколько раз (subPath mount) — для
    edge это значения не имеет, фильтруем здесь.
    """
    seen: List[str] = []
    spec = pod.get("spec") or {}
    for vol in spec.get("volumes") or []:
        pvc = vol.get("persistentVolumeClaim") or {}
        claim = pvc.get("claimName")
        if claim and claim not in seen:
            seen.append(claim)
    return seen


# ── upsert helpers ──────────────────────────────────────────────────────────


def _upsert_volume(
    db: Session,
    fields: Dict[str, Any],
    disk_pct: Optional[float] = None,
) -> StorageVolume:
    """Идемпотентный upsert по (kind, namespace, name)."""
    vol = (
        db.query(StorageVolume)
        .filter_by(
            kind=fields["kind"],
            namespace=fields["namespace"],
            name=fields["name"],
        )
        .one_or_none()
    )
    if vol is None:
        vol = StorageVolume(
            kind=fields["kind"],
            namespace=fields["namespace"],
            name=fields["name"],
            capacity_bytes=fields.get("capacity_bytes"),
            storage_class=fields.get("storage_class"),
            phase=fields.get("phase"),
            access_modes=fields.get("access_modes"),
            volume_name=fields.get("volume_name"),
            metadata_json=fields.get("metadata_json"),
            disk_pct=disk_pct,
        )
        # SAVEPOINT: параллельный beat-tick мог вставить ту же строку между
        # one_or_none() и flush() → INSERT упрётся в UNIQUE
        # (uq_kg_storage_volumes_kind_ns_name). begin_nested откатывает только
        # этот INSERT (не весь tick); затем перечитываем победителя и апдейтим
        # его как existing. См. k8s_events_sync per-item SAVEPOINT.
        try:
            with db.begin_nested():
                db.add(vol)
                db.flush()
            return vol
        except IntegrityError:
            vol = (
                db.query(StorageVolume)
                .filter_by(
                    kind=fields["kind"],
                    namespace=fields["namespace"],
                    name=fields["name"],
                )
                .one()
            )
            # проваливаемся в update-путь ниже (last-write-wins).

    # Update mutable fields. Не трогаем disk_pct если на этом тике нет
    # данных (None) — чтобы предыдущее значение не стиралось при
    # STORAGE_METRICS_ENABLED=False.
    vol.capacity_bytes = cast(Any, fields.get("capacity_bytes"))
    vol.storage_class = cast(Any, fields.get("storage_class"))
    vol.phase = cast(Any, fields.get("phase"))
    vol.access_modes = cast(Any, fields.get("access_modes"))
    vol.volume_name = cast(Any, fields.get("volume_name"))
    vol.metadata_json = cast(Any, fields.get("metadata_json"))
    if disk_pct is not None:
        vol.disk_pct = cast(Any, disk_pct)
    db.flush()
    return vol


def _upsert_volume_edge(
    db: Session,
    src_kind: str,
    src_id: int,
    dst_kind: str,
    dst_id: int,
    kind: str,
    discovered_by: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> VolumeEdge:
    """Идемпотентный upsert по (src_kind, src_id, dst_kind, dst_id, kind).

    last_seen_at обновляется на каждом вызове — даёт основу для будущего
    decay-task'а (edges не подтверждённые N дней соответствуют удалённым
    PVC/Pod'ам).
    """
    edge = (
        db.query(VolumeEdge)
        .filter_by(
            src_kind=src_kind, src_id=src_id,
            dst_kind=dst_kind, dst_id=dst_id, kind=kind,
        )
        .one_or_none()
    )
    now = datetime.utcnow()
    if edge is None:
        new_edge = VolumeEdge(
            src_kind=src_kind, src_id=src_id,
            dst_kind=dst_kind, dst_id=dst_id,
            kind=kind,
            discovered_by=discovered_by,
            extras=extras or None,
            last_seen_at=now,
        )
        # SAVEPOINT: гонка параллельных tick'ов на UNIQUE
        # (uq_kg_volume_edge_src_dst_kind) — INSERT после чужого INSERT'а
        # упал бы IntegrityError'ом и убил tick. begin_nested откатывает
        # только этот INSERT; далее перечитываем победителя и обновляем его
        # last_seen_at/extras (update-путь ниже). См. _upsert_volume.
        try:
            with db.begin_nested():
                db.add(new_edge)
                db.flush()
            return new_edge
        except IntegrityError:
            edge = (
                db.query(VolumeEdge)
                .filter_by(
                    src_kind=src_kind, src_id=src_id,
                    dst_kind=dst_kind, dst_id=dst_id, kind=kind,
                )
                .one()
            )
            # проваливаемся в update-путь ниже.

    edge.last_seen_at = cast(Any, now)
    if extras:
        merged = dict(edge.extras or {})
        merged.update(extras)
        if merged != (edge.extras or {}):
            edge.extras = cast(Any, merged)
    db.flush()
    return edge


# ── disk_pct enrichment (optional) ──────────────────────────────────────────


async def _fetch_disk_pct_map() -> Dict[Tuple[str, str], float]:
    """VM query → map (namespace, pvc_name) → disk_pct.

    Только если STORAGE_METRICS_ENABLED И VICTORIA_METRICS_URL непустой.
    Возвращает {} при любой ошибке / no-data — disk_pct поле остаётся
    нетронутым (см. _upsert_volume).

    Запрос cluster-wide, один shot — на 60 ns × N pvc дешевле чем N
    point-запросов. Если scrape config kubelet_volume_stats_* не настроен,
    result будет [] и map пустая.
    """
    if not settings.STORAGE_METRICS_ENABLED:
        return {}
    if not settings.VICTORIA_METRICS_URL:
        logger.info("k8s_storage.disk_pct_skipped reason=no_vm_url")
        return {}

    from app.context.vm_client import VMClient

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
    # `_disk_pct_query_series` парсит series — VMClient.query_instant
    # сворачивает в одно число, что нам не подходит. Сделаем raw GET.
    import httpx
    query = (
        '100 * kubelet_volume_stats_used_bytes '
        '/ kubelet_volume_stats_capacity_bytes'
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{vm._url}/api/v1/query", params={"query": query},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("k8s_storage.disk_pct_query_failed err=%s", e)
        return {}

    out: Dict[Tuple[str, str], float] = {}
    for series in (data.get("data") or {}).get("result") or []:
        metric = series.get("metric") or {}
        ns = metric.get("namespace")
        pvc_name = metric.get("persistentvolumeclaim")
        value = (series.get("value") or [None, None])[1]
        if not ns or not pvc_name or value is None:
            continue
        try:
            out[(ns, pvc_name)] = float(value)
        except (TypeError, ValueError):
            continue
    logger.info("k8s_storage.disk_pct_fetched count=%d", len(out))
    return out


def _fetch_disk_pct_map_sync() -> Dict[Tuple[str, str], float]:
    """Sync обёртка для Celery (sync-context). Внутри asyncio.run."""
    import asyncio
    return asyncio.run(_fetch_disk_pct_map())


# ── main syncs ──────────────────────────────────────────────────────────────


def sync_pvs(db: Session) -> Dict[str, int]:
    """Загрузить все PVs cluster-wide и upsert как kg_storage_volumes(kind='pv')."""
    pvs = _kubectl_get_all("persistentvolumes")
    stats = {
        "pvs_fetched": len(pvs),
        "pvs_upserted": 0,
        "pvs_skipped": 0,
    }
    for pv in pvs:
        fields = _extract_pv_fields(pv)
        if not fields["name"]:
            stats["pvs_skipped"] += 1
            continue
        _upsert_volume(db, fields)
        stats["pvs_upserted"] += 1
    db.commit()
    logger.info(
        "k8s_storage.pvs_done fetched=%d upserted=%d skipped=%d",
        stats["pvs_fetched"], stats["pvs_upserted"], stats["pvs_skipped"],
    )
    return stats


def sync_pvcs(
    db: Session,
    disk_pct_map: Optional[Dict[Tuple[str, str], float]] = None,
) -> Dict[str, int]:
    """Загрузить все PVCs cluster-wide.

    Делает upsert kg_storage_volumes(kind='pvc') + edge `bound_to`
    (PVC → PV) когда pvc.spec.volumeName указан. Сам PV должен уже быть в
    KG (sync_pvs предшествует). Если PV нет — edge не создаётся, считаем
    в `skipped_unknown_pv`.

    `disk_pct_map` (ns, pvc_name) → % из VM, опционально. None == все
    PVC получат disk_pct=None (или сохранят прошлое значение).
    """
    pvcs = _kubectl_get_all("persistentvolumeclaims")
    disk_pct_map = disk_pct_map or {}
    stats = {
        "pvcs_fetched": len(pvcs),
        "pvcs_upserted": 0,
        "pvcs_skipped": 0,
        "edges_bound_to": 0,
        "skipped_unknown_pv": 0,
        "disk_pct_attached": 0,
    }
    for pvc in pvcs:
        fields = _extract_pvc_fields(pvc)
        if not fields["name"]:
            stats["pvcs_skipped"] += 1
            continue
        disk_pct = disk_pct_map.get((fields["namespace"], fields["name"]))
        if disk_pct is not None:
            stats["disk_pct_attached"] += 1
        pvc_node = _upsert_volume(db, fields, disk_pct=disk_pct)
        stats["pvcs_upserted"] += 1

        # bound_to PV
        bound_pv_name = fields.get("volume_name")
        if bound_pv_name:
            pv_node = (
                db.query(StorageVolume)
                .filter_by(kind="pv", namespace="", name=bound_pv_name)
                .one_or_none()
            )
            if pv_node is None:
                stats["skipped_unknown_pv"] += 1
                continue
            _upsert_volume_edge(
                db,
                src_kind=NODE_PVC, src_id=cast(int, pvc_node.id),
                dst_kind=NODE_PV, dst_id=cast(int, pv_node.id),
                kind=EDGE_BOUND_TO,
                discovered_by=DISCOVERED_BY_PVC_SPEC,
                extras={"phase": fields.get("phase")},
            )
            stats["edges_bound_to"] += 1
    db.commit()
    logger.info(
        "k8s_storage.pvcs_done fetched=%d upserted=%d edges_bound=%d "
        "unknown_pv=%d disk_pct=%d",
        stats["pvcs_fetched"], stats["pvcs_upserted"],
        stats["edges_bound_to"], stats["skipped_unknown_pv"],
        stats["disk_pct_attached"],
    )
    return stats


def sync_pod_pvc_edges(db: Session) -> Dict[str, int]:
    """Scan all pods → создать `uses_volume` edges (Service → PVC).

    Алгоритм:
      1. kubectl get pods -A, kubectl get rs -A (для resolution Pod→Deploy).
      2. Для каждого pod-а:
         - резолвим owning workload (StS/DS/Deploy/Job) через ownerRef chain;
         - находим matching kg_services row (namespace + name);
         - для каждого claimName в pod.spec.volumes создаём edge.
      3. PVC node-resolve: kg_storage_volumes filter (kind='pvc', namespace, name).
         Если PVC ещё не в KG (sync_pvcs не отработал) — увеличиваем
         `skipped_unknown_pvc`, не raise.

    Edges идемпотентны через _upsert_volume_edge (UNIQUE).
    """
    pods = _kubectl_get_all("pods")
    replicasets = _kubectl_get_all("replicasets")
    rs_index = _build_rs_to_deployment_index(replicasets)

    stats = {
        "pods_scanned": len(pods),
        "pods_with_pvcs": 0,
        "claim_refs_seen": 0,
        "edges_uses_volume": 0,
        "skipped_no_owner": 0,
        "skipped_no_service": 0,
        "skipped_unknown_pvc": 0,
    }

    for pod in pods:
        claims = _pod_pvc_claims(pod)
        if not claims:
            continue
        stats["pods_with_pvcs"] += 1
        stats["claim_refs_seen"] += len(claims)

        owner = _pod_owner_chain_to_deployment(pod, rs_index)
        if not owner:
            stats["skipped_no_owner"] += 1
            continue
        ns, workload_name = owner

        svc = (
            db.query(Service)
            .filter_by(namespace=ns, name=workload_name, node_kind=NODE_KIND_SERVICE)
            .one_or_none()
        )
        if svc is None:
            # Workload видим в k8s, но в KG ещё нет (kg_topology_sync не
            # подхватил). Не плодим фантом-узлы — следующий tick догонит.
            stats["skipped_no_service"] += 1
            continue

        for claim_name in claims:
            pvc_node = (
                db.query(StorageVolume)
                .filter_by(kind="pvc", namespace=ns, name=claim_name)
                .one_or_none()
            )
            if pvc_node is None:
                stats["skipped_unknown_pvc"] += 1
                continue
            _upsert_volume_edge(
                db,
                src_kind=NODE_SERVICE, src_id=cast(int, svc.id),
                dst_kind=NODE_PVC, dst_id=cast(int, pvc_node.id),
                kind=EDGE_USES_VOLUME,
                discovered_by=DISCOVERED_BY_PODS,
            )
            stats["edges_uses_volume"] += 1

    db.commit()
    logger.info(
        "k8s_storage.pod_edges_done pods=%d with_pvcs=%d edges=%d "
        "no_owner=%d no_service=%d unknown_pvc=%d",
        stats["pods_scanned"], stats["pods_with_pvcs"],
        stats["edges_uses_volume"], stats["skipped_no_owner"],
        stats["skipped_no_service"], stats["skipped_unknown_pvc"],
    )
    return stats


def sync_storage(db: Session) -> Dict[str, Any]:
    """Main entry: PV → PVC → pod edges → disk_pct enrichment.

    Порядок важен:
      1. PV сначала — чтобы PVC.bound_to мог разрешить FK по volume_name;
      2. PVC — disk_pct прикрепляется здесь;
      3. Pod edges последними — нужны PVC-nodes в KG для (ns, name) lookup.
    """
    disk_pct_map: Dict[Tuple[str, str], float] = {}
    try:
        disk_pct_map = _fetch_disk_pct_map_sync()
    except Exception as e:
        logger.warning("k8s_storage.disk_pct_fetch_failed err=%s", e)

    pv_stats = sync_pvs(db)
    pvc_stats = sync_pvcs(db, disk_pct_map=disk_pct_map)
    edge_stats = sync_pod_pvc_edges(db)

    return {
        "pvs": pv_stats,
        "pvcs": pvc_stats,
        "pod_edges": edge_stats,
        "disk_pct_enabled": bool(settings.STORAGE_METRICS_ENABLED),
        "disk_pct_series": len(disk_pct_map),
    }


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = sync_storage(db)
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
