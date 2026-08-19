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

* **Убыль** (decay/cleanup) — см. `decay_volume_edges` и
  `_cleanup_absent_volumes`: удалённый в кластере PVC обязан уйти и из графа,
  иначе он вечно тянется в blast-radius. Обе чистки source-aware: если
  соответствующий срез kubectl в этом цикле сбойнул, ничего не удаляем
  (пустой/частичный fetch неотличим от «в кластере больше нет объектов» —
  ровно тот класс инцидента, который описан в `edge_decay_guard`).

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
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, cast

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.edge_decay_guard import (
    SOURCE_STORAGE_PODS, SOURCE_STORAGE_PVCS, record_source_run,
    unhealthy_volume_sources, volume_edge_block_reason,
)
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service, StorageVolume, VolumeEdge

logger = logging.getLogger(__name__)

# Таймаут одного `kubectl get`. Было 30с — и это заведомо мало для
# cluster-wide листов: в этом же репо задокументировано, что
# `kubectl get deployments -A -o json` = 2318 объектов / ~42 МБ pretty-JSON и
# внутри пода воркера в 30с НЕ укладывался (см. комментарий к
# `k8s_topology_resources_sync._KUBECTL_TIMEOUT_S` — там из-за этого
# services_fetched=0 каждый тик). Листы pods/replicasets заведомо больше
# deployments, а расплата тут та же: uses_volume-рёбра не создаются и не
# освежаются. Берём тот же порог 180с, что у топологии.
_KUBECTL_TIMEOUT_S = 180

# Пагинация листа: apiserver отдаёт по _KUBECTL_CHUNK объектов за запрос,
# kubectl склеивает. Пиковая стоимость одного round-trip падает на порядок,
# и большой лист не упирается в один таймаут целиком. Значение — как в
# k8s_topology_resources_sync (там это уже проверено на этом кластере).
_KUBECTL_CHUNK = 500

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

# Порог «ребро не подтверждено синком» для decay kg_volume_edges. Синк ходит
# каждые 30 минут, так что 48ч = ~96 пропущенных подтверждений — столько
# длится только реальная пропажа объекта, а не одиночный сбой. Значение
# зеркалит KG_K8S_JOBS_STALE_HOURS (k8s_jobs_sync.cleanup_stale_jobs).
_VOLUME_EDGE_STALE_HOURS_DEFAULT = 48

# Cap на объём одной чистки (и рёбер, и узлов): массовая «пропажа» — симптом
# сбоя, а не реальной убыли storage. Тот же порог, что у
# kg_sync.EDGE_DECAY_MAX_DELETE_PCT.
_VOLUME_MAX_DELETE_PCT = 25.0

# Размер порции для `IN (...)`-удалений: не упираемся в лимит параметров
# драйвера на больших чистках.
_DELETE_CHUNK = 500

# Причины пропуска чистки (уходят в stats и в логи).
REASON_FETCH_FAILED = "fetch_failed"
REASON_EMPTY_FETCH = "empty_fetch"
REASON_DELETE_PCT = "delete_pct"


class KubectlFetchError(RuntimeError):
    """kubectl реально упал (rc!=0 / timeout / битый JSON) — в отличие от
    валидного пустого листа.

    Зеркалит `kg_sync.KubectlFetchError`. Раньше `_kubectl_get_all` на любой
    сбой возвращал `[]`, и отказ был НЕОТЛИЧИМ от «в кластере нет объектов»:
    в stats уходило `pods_scanned=0` без единой ошибки, uses_volume-рёбра не
    создавались и не освежались, а выглядело это как «нет данных». Теперь
    сбой сигналится исключением, вызывающий инкрементит `errors`, отдаёт это
    в stats и в `record_source_run` — decay видит нездоровый источник и не
    трогает его рёбра.
    """


# ── kubectl wrappers ────────────────────────────────────────────────────────


def _kubectl_get_all(resource: str) -> List[Dict[str, Any]]:
    """`kubectl get <resource> -A -o json --chunk-size=N` → items list.

    Raises `KubectlFetchError` при РЕАЛЬНОМ сбое (timeout / rc!=0 / битый
    JSON). Пустой список = в кластере действительно нет таких объектов.
    Ловить исключение — дело вызывающего (см. `_fetch_items`): beat-loop
    валить нельзя, но и молчать об отказе тоже.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", resource, "-A", "-o", "json",
             f"--chunk-size={_KUBECTL_CHUNK}"],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise KubectlFetchError(
            f"kubectl get {resource} timeout={_KUBECTL_TIMEOUT_S}s",
        ) from e
    except Exception as e:
        raise KubectlFetchError(f"kubectl get {resource} failed: {e}") from e

    if out.returncode != 0:
        raise KubectlFetchError(
            f"kubectl get {resource} rc={out.returncode} "
            f"stderr={(out.stderr or '').strip()[:200]}",
        )

    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as e:
        raise KubectlFetchError(
            f"kubectl get {resource} invalid json: {e}",
        ) from e

    items = data.get("items") or []
    return items if isinstance(items, list) else []


# ── проекция полей: почему листы pods/replicasets нельзя тянуть целиком ─────
#
# 19.08.2026: `copilot-worker` OOMKilled (exit 137) 57 раз за 48 часов, ровно
# 1-2 раза в час, при limits memory=3Gi. Виновник — этот синк: смерти
# кластеризовались в :27-:28 и :57-:58, т.е. через 1-2 минуты после его старта
# (`crontab(minute="26,56")`), а память пода скакала с ~550 MiB до 2.1-2.4 GiB
# за одну минуту и упиралась в лимит. В покое воркер держит 290-600 MiB, так
# что это не утечка, а пиковая аллокация одного тика.
#
# Замер на этом кластере (19.08.2026):
#   kubectl get pods -A -o json          =  95.8 МБ   (4 919 объектов)
#   kubectl get replicasets -A -o json   = 209.3 МБ  (11 279 объектов)
#   persistentvolumeclaims / -volumes    = 2.1 / 2.7 МБ (мелочь)
# `subprocess.run(capture_output=True, text=True)` держит весь stdout строкой,
# `json.loads` поверх этого даёт ещё в разы больше на dict/list-объекты — вот
# и 2+ GiB на 305 МБ входа. Причём RS в 2 раза тяжелее подов: 11 279 RS при
# 4 919 подах — это хвост старых ревизий (revisionHistoryLimit не подрезан).
#
# При этом синку нужны ЧЕТЫРЕ поля: у пода — namespace, ownerReferences и
# claimName из spec.volumes; у RS — namespace, name, ownerReferences. Поэтому
# листы идут постранично через kube API (limit/continue) и каждая страница
# СРАЗУ сжимается до этих полей — в памяти живёт одна страница, а не кластер.
_PAGE_LIMIT = 500


def _project_pod(item: Dict[str, Any]) -> Dict[str, Any]:
    """Оставить от пода только то, что читают `_pod_pvc_claims` и owner-chain."""
    meta = item.get("metadata") or {}
    vols = []
    for vol in (item.get("spec") or {}).get("volumes") or []:
        claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if claim:
            vols.append({"persistentVolumeClaim": {"claimName": claim}})
    return {
        "metadata": {
            "namespace": meta.get("namespace"),
            "name": meta.get("name"),
            "ownerReferences": meta.get("ownerReferences") or [],
        },
        "spec": {"volumes": vols},
    }


def _project_replicaset(item: Dict[str, Any]) -> Dict[str, Any]:
    """Оставить от RS только то, что читает `_build_rs_to_deployment_index`."""
    meta = item.get("metadata") or {}
    return {
        "metadata": {
            "namespace": meta.get("namespace"),
            "name": meta.get("name"),
            "ownerReferences": meta.get("ownerReferences") or [],
        },
    }


# resource → (путь листа в kube API, проекция страницы)
_PAGED: Dict[str, Tuple[str, Any]] = {
    "pods": ("/api/v1/pods", _project_pod),
    "replicasets": ("/apis/apps/v1/replicasets", _project_replicaset),
}


def _list_paged_projected(resource: str) -> List[Dict[str, Any]]:
    """Постраничный лист через kube API с проекцией каждой страницы.

    Raises `KubectlFetchError` при любом сбое — вызывающий (`_fetch_items`)
    трактует его так же, как отказ kubectl: beat-loop не валим, но и молчать
    об отказе нельзя.

    Идёт напрямую по REST-пути с `_preload_content=False`: так страница
    парсится из сырого JSON apiserver-а (тот же camelCase, что у `kubectl -o
    json`), без построения V1Pod/V1ReplicaSet-моделей — они на 500 объектов
    стоят дороже, чем сам dict.
    """
    path, project = _PAGED[resource]
    # Локальный импорт — kubernetes-client не нужен на путях, где этот модуль
    # только парсит уже готовые dict-ы (тесты, CLI над фикстурами).
    try:
        from kubernetes import client as k8s_client

        from app.context.deployments import _load_k8s_once
    except Exception as e:
        raise KubectlFetchError(f"kubernetes client unavailable: {e}") from e

    if not _load_k8s_once():
        raise KubectlFetchError("kube-config unavailable")

    api = k8s_client.ApiClient()
    items: List[Dict[str, Any]] = []
    token: Optional[str] = None
    pages = 0
    while True:
        params: List[Tuple[str, Any]] = [("limit", _PAGE_LIMIT)]
        if token:
            params.append(("continue", token))
        try:
            resp = api.call_api(
                path, "GET",
                query_params=params,
                header_params={"Accept": "application/json"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=_KUBECTL_TIMEOUT_S,
            )
            raw = resp[0].data if isinstance(resp, tuple) else resp.data
            page = json.loads(raw)
        except Exception as e:
            raise KubectlFetchError(f"list {resource} page={pages} failed: {e}") from e

        for item in page.get("items") or []:
            items.append(project(item))
        pages += 1
        token = ((page.get("metadata") or {}).get("continue")) or None
        if not token:
            break
    logger.debug(
        "k8s_storage.listed resource=%s items=%d pages=%d", resource, len(items), pages,
    )
    return items


def _get_all(resource: str) -> List[Dict[str, Any]]:
    """Единая точка фетча листа: постранично+проекция там, где лист тяжёлый.

    pods/replicasets — через `_list_paged_projected` (см. комментарий выше про
    OOM). pvc/pv остаются на `kubectl`: вместе они дают ~5 МБ, а их
    `_extract_*_fields` читают много полей — проецировать нечего, риск
    потерять поле есть.
    """
    if resource in _PAGED:
        return _list_paged_projected(resource)
    return _kubectl_get_all(resource)


def _fetch_items(
    resource: str,
    stats: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Фетч листа с УЧЁТОМ отказа: (items, fetch_ok).

    При сбое — `errors += 1`, `fetch_failed = True` в переданном stats
    (оттуда они попадают и в результат таска, и в `record_source_run`), и
    возвращается ([], False). Вызывающий по `fetch_ok` решает, можно ли
    что-то чистить по этому снимку.
    """
    try:
        return _get_all(resource), True
    except KubectlFetchError as e:
        stats["errors"] = int(stats.get("errors") or 0) + 1
        stats["fetch_failed"] = True
        logger.warning(
            "k8s_storage.fetch_failed resource=%s err=%s — отказ учтён в "
            "errors, чистка по этому снимку пропущена",
            resource, e,
        )
        return [], False


def _chunked(seq: Sequence[int], size: int = _DELETE_CHUNK) -> Iterator[Sequence[int]]:
    """Порции для `IN (...)`: длинный список id в один запрос не влезает."""
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


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

    last_seen_at обновляется на каждом вызове — на нём и стоит decay
    (`decay_volume_edges`): ребро, не подтверждённое N часов, соответствует
    удалённому PVC/Pod'у.
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


# ── decay / cleanup ─────────────────────────────────────────────────────────
#
# Убыль storage раньше не обрабатывалась ВООБЩЕ (чистка была помечена «в
# будущем»): PVC удаляли из кластера, а узел и его `uses_volume`/`bound_to`
# рёбра оставались в графе навсегда и продолжали попадать в blast-radius.
# Здесь две чистки, обе по дисциплине edge_decay_guard / cleanup_stale_jobs:
#
#   1. `_cleanup_absent_volumes` — узлы: снимок cluster-wide листа у нас на
#      руках, поэтому строку, которой в снимке нет, можно удалять сразу
#      (плюс её рёбра — FK у tagged-id нет, каскада БД не будет).
#   2. `decay_volume_edges` — рёбра по `last_seen_at`: ловит случай, когда сам
#      PVC жив, а pod, который его монтировал, исчез.
#
# ОБЕ отказываются работать, если источник признан нездоровым: сбойный fetch
# неотличим от опустевшего кластера, и удаление по нему = та самая «тихая
# эрозия рёбер».


def _volume_edge_stale_hours() -> int:
    try:
        return int(getattr(
            settings, "KG_VOLUME_EDGE_STALE_HOURS",
            _VOLUME_EDGE_STALE_HOURS_DEFAULT,
        ))
    except (TypeError, ValueError):
        return _VOLUME_EDGE_STALE_HOURS_DEFAULT


def _delete_edges_for_volume_ids(
    db: Session, node_kind: str, ids: Sequence[int],
) -> int:
    """Удалить рёбра, висящие на удаляемых узлах (src ИЛИ dst).

    FK у tagged-id (`src_kind` + `src_id`) нет — БД каскадом не подчистит,
    иначе рёбра остались бы висеть на несуществующих узлах. `node_kind`
    совпадает со `StorageVolume.kind` (значения 'pvc'/'pv' — это и есть
    namespacing tagged-id, см. NODE_PVC / NODE_PV).
    """
    deleted = 0
    for part in _chunked(list(ids)):
        deleted += int(
            db.query(VolumeEdge)
            .filter(
                or_(
                    and_(VolumeEdge.src_kind == node_kind,
                         VolumeEdge.src_id.in_(part)),
                    and_(VolumeEdge.dst_kind == node_kind,
                         VolumeEdge.dst_id.in_(part)),
                ),
            )
            .delete(synchronize_session=False)
            or 0
        )
    return deleted


def _cleanup_absent_volumes(
    db: Session,
    *,
    kind: str,
    seen: Set[Tuple[str, str]],
    fetch_ok: bool,
) -> Dict[str, Any]:
    """Удалить узлы `kg_storage_volumes` данного kind, которых нет в снимке.

    `seen` — множество (namespace, name) из ТЕКУЩЕГО cluster-wide листа.
    Пропускаем чистку, если:
      * `fetch_ok=False` — kubectl сбойнул, снимок неполный;
      * `seen` пусто — пустой fetch неотличим от пустого кластера
        (дисциплина `k8s_jobs_sync.cleanup_stale_jobs`);
      * удаление затронуло бы > `_VOLUME_MAX_DELETE_PCT`% узлов kind'а.
    """
    stats: Dict[str, Any] = {
        "volumes_deleted": 0, "edges_deleted": 0, "skipped": "",
    }
    if not fetch_ok:
        stats["skipped"] = REASON_FETCH_FAILED
        logger.warning(
            "k8s_storage.volume_cleanup_skipped kind=%s reason=%s — снимок "
            "неполный, узлы не чистим",
            kind, REASON_FETCH_FAILED,
        )
        return stats
    if not seen:
        stats["skipped"] = REASON_EMPTY_FETCH
        logger.warning(
            "k8s_storage.volume_cleanup_skipped kind=%s reason=%s — 0 объектов "
            "в листе (сбой неотличим от пустого кластера)",
            kind, REASON_EMPTY_FETCH,
        )
        return stats

    rows = (
        db.query(StorageVolume.id, StorageVolume.namespace, StorageVolume.name)
        .filter(StorageVolume.kind == kind)
        .all()
    )
    if not rows:
        return stats
    absent = [
        int(row_id) for row_id, ns, name in rows
        if ((ns or ""), (name or "")) not in seen
    ]
    if not absent:
        return stats

    delete_pct = 100.0 * len(absent) / len(rows)
    if delete_pct > _VOLUME_MAX_DELETE_PCT:
        stats["skipped"] = REASON_DELETE_PCT
        logger.warning(
            "k8s_storage.volume_cleanup_skipped kind=%s reason=%s "
            "would_delete=%d of %d (%.1f%% > %.1f%%) — массовая пропажа это "
            "симптом сбоя, не убыли storage",
            kind, REASON_DELETE_PCT, len(absent), len(rows),
            delete_pct, _VOLUME_MAX_DELETE_PCT,
        )
        return stats

    stats["edges_deleted"] = _delete_edges_for_volume_ids(db, kind, absent)
    deleted = 0
    for part in _chunked(absent):
        deleted += int(
            db.query(StorageVolume)
            .filter(StorageVolume.id.in_(part))
            .delete(synchronize_session=False)
            or 0
        )
    stats["volumes_deleted"] = deleted
    logger.info(
        "k8s_storage.volume_cleanup kind=%s deleted=%d edges_deleted=%d of %d",
        kind, deleted, stats["edges_deleted"], len(rows),
    )
    return stats


def _stale_edge_clause(cutoff: datetime) -> Any:
    """«Ребро не подтверждено с cutoff». last_seen_at NULL у legacy-строк
    (колонка nullable) — для них судим по created_at."""
    return or_(
        and_(VolumeEdge.last_seen_at.isnot(None),
             VolumeEdge.last_seen_at < cutoff),
        and_(VolumeEdge.last_seen_at.is_(None),
             VolumeEdge.created_at < cutoff),
    )


def decay_volume_edges(
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decay `kg_volume_edges` по last_seen_at. Возвращает stats.

    Source-aware, ровно как decay `kg_service_edges`: рёбра kind'а, чей срез
    kubectl в этом цикле упал / отдал подозрительный ноль / давно не освежал
    ни одного своего ребра, из чистки ИСКЛЮЧАЮТСЯ (см.
    `edge_decay_guard.unhealthy_volume_sources`). Fail-closed: kind, не
    сопоставленный источнику, не децаится вовсе. Любой пропуск логируется
    warning'ом — молчаливая эрозия и была исходной бедой.

    Плюс cap `_VOLUME_MAX_DELETE_PCT`: массовое удаление отменяет весь проход.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=_volume_edge_stale_hours())
    stats: Dict[str, Any] = {
        "deleted": 0, "skipped_decay": 0, "reason": "",
        "stale_sources": [], "blocked_by_source": 0, "blocked_kinds": {},
    }

    unhealthy = unhealthy_volume_sources(db, now)
    stats["stale_sources"] = sorted(unhealthy)

    total = int(db.query(VolumeEdge).count() or 0)
    candidates = (
        db.query(VolumeEdge).filter(_stale_edge_clause(cutoff)).all()
        if total else []
    )

    blocked: Dict[str, Dict[str, int]] = {}
    eligible: List[int] = []
    for edge in candidates:
        reason = volume_edge_block_reason(
            cast(Optional[str], edge.kind),
            cast(Optional[str], edge.discovered_by),
            unhealthy,
        )
        if reason:
            by_kind = blocked.setdefault(reason, {})
            kind_name = cast(Optional[str], edge.kind) or "unknown"
            by_kind[kind_name] = by_kind.get(kind_name, 0) + 1
            stats["blocked_by_source"] += 1
        else:
            eligible.append(cast(int, edge.id))

    stats["blocked_kinds"] = {r: dict(k) for r, k in blocked.items()}
    for why, by_kind in sorted(blocked.items()):
        logger.warning(
            "k8s_storage.volume_decay_blocked reason=%s kinds=%s "
            "blocked_edges=%d — срез, отвечающий за свежесть этих рёбер, в "
            "этом цикле не отработал; decay для них пропущен",
            why, sorted(by_kind), sum(by_kind.values()),
        )

    if not eligible:
        return stats

    delete_pct = 100.0 * len(eligible) / total if total else 0.0
    if delete_pct > _VOLUME_MAX_DELETE_PCT:
        stats["skipped_decay"] = 1
        stats["reason"] = REASON_DELETE_PCT
        logger.warning(
            "k8s_storage.volume_decay_skipped reason=%s total=%d "
            "would_delete=%d (%.1f%% > %.1f%%)",
            REASON_DELETE_PCT, total, len(eligible),
            delete_pct, _VOLUME_MAX_DELETE_PCT,
        )
        return stats

    # Условие возраста ПОВТОРЯЕТСЯ в WHERE самого DELETE: конкурентный синк
    # мог освежить ребро между SELECT и DELETE (то же, что в
    # kg_sync._decay_stale_edges).
    deleted = 0
    for part in _chunked(eligible):
        deleted += int(
            db.query(VolumeEdge)
            .filter(VolumeEdge.id.in_(part), _stale_edge_clause(cutoff))
            .delete(synchronize_session=False)
            or 0
        )
    stats["deleted"] = deleted
    logger.info(
        "k8s_storage.volume_decay deleted=%d of total=%d stale_after=%dh "
        "blocked=%d",
        deleted, total, _volume_edge_stale_hours(), stats["blocked_by_source"],
    )
    return stats


# ── main syncs ──────────────────────────────────────────────────────────────


def sync_pvs(db: Session) -> Dict[str, Any]:
    """Загрузить все PVs cluster-wide и upsert как kg_storage_volumes(kind='pv').

    Сбой fetch'а виден в stats (`errors` / `fetch_failed`), а не маскируется
    под «0 объектов»: по неполному снимку чистка узлов не делается.
    """
    stats: Dict[str, Any] = {
        "pvs_fetched": 0,
        "pvs_upserted": 0,
        "pvs_skipped": 0,
        "errors": 0,
        "fetch_failed": False,
    }
    pvs, fetch_ok = _fetch_items("persistentvolumes", stats)
    stats["pvs_fetched"] = len(pvs)
    seen: Set[Tuple[str, str]] = set()
    for pv in pvs:
        fields = _extract_pv_fields(pv)
        if not fields["name"]:
            stats["pvs_skipped"] += 1
            continue
        _upsert_volume(db, fields)
        seen.add((fields["namespace"], fields["name"]))
        stats["pvs_upserted"] += 1
    cleanup = _cleanup_absent_volumes(
        db, kind=NODE_PV, seen=seen, fetch_ok=fetch_ok,
    )
    stats["cleanup"] = cleanup
    db.commit()
    logger.info(
        "k8s_storage.pvs_done fetched=%d upserted=%d skipped=%d errors=%d "
        "cleaned=%d",
        stats["pvs_fetched"], stats["pvs_upserted"], stats["pvs_skipped"],
        stats["errors"], cleanup["volumes_deleted"],
    )
    return stats


def sync_pvcs(
    db: Session,
    disk_pct_map: Optional[Dict[Tuple[str, str], float]] = None,
) -> Dict[str, Any]:
    """Загрузить все PVCs cluster-wide.

    Делает upsert kg_storage_volumes(kind='pvc') + edge `bound_to`
    (PVC → PV) когда pvc.spec.volumeName указан. Сам PV должен уже быть в
    KG (sync_pvs предшествует). Если PV нет — edge не создаётся, считаем
    в `skipped_unknown_pv`.

    `disk_pct_map` (ns, pvc_name) → % из VM, опционально. None == все
    PVC получат disk_pct=None (или сохранят прошлое значение).

    Отчитывается в `edge_decay_guard` как источник свежести `bound_to`: если
    этот срез сбойнул, decay не тронет его рёбра.
    """
    disk_pct_map = disk_pct_map or {}
    stats: Dict[str, Any] = {
        "pvcs_fetched": 0,
        "pvcs_upserted": 0,
        "pvcs_skipped": 0,
        "edges_bound_to": 0,
        "skipped_unknown_pv": 0,
        "disk_pct_attached": 0,
        "errors": 0,
        "fetch_failed": False,
    }
    pvcs, fetch_ok = _fetch_items("persistentvolumeclaims", stats)
    stats["pvcs_fetched"] = len(pvcs)
    seen: Set[Tuple[str, str]] = set()
    for pvc in pvcs:
        fields = _extract_pvc_fields(pvc)
        if not fields["name"]:
            stats["pvcs_skipped"] += 1
            continue
        disk_pct = disk_pct_map.get((fields["namespace"], fields["name"]))
        if disk_pct is not None:
            stats["disk_pct_attached"] += 1
        pvc_node = _upsert_volume(db, fields, disk_pct=disk_pct)
        seen.add((fields["namespace"], fields["name"]))
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
    cleanup = _cleanup_absent_volumes(
        db, kind=NODE_PVC, seen=seen, fetch_ok=fetch_ok,
    )
    stats["cleanup"] = cleanup
    db.commit()
    logger.info(
        "k8s_storage.pvcs_done fetched=%d upserted=%d edges_bound=%d "
        "unknown_pv=%d disk_pct=%d errors=%d cleaned=%d cleaned_edges=%d",
        stats["pvcs_fetched"], stats["pvcs_upserted"],
        stats["edges_bound_to"], stats["skipped_unknown_pv"],
        stats["disk_pct_attached"], stats["errors"],
        cleanup["volumes_deleted"], cleanup["edges_deleted"],
    )
    # Отчёт для decay-guard: этот срез отвечает за свежесть `bound_to`.
    # Ровно тут и ломался бы прод при таймауте листа: pvcs_fetched=0 без
    # errors выглядел бы как «PVC в кластере не осталось».
    record_source_run(SOURCE_STORAGE_PVCS, stats)
    return stats


def sync_pod_pvc_edges(db: Session) -> Dict[str, Any]:
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

    Это САМЫЙ тяжёлый fetch модуля (лист pod'ов cluster-wide). Отказ любого
    из двух срезов уходит в `errors`/`fetch_failed` и в отчёт
    `SOURCE_STORAGE_PODS`: раньше таймаут давал `pods_scanned=0` БЕЗ ошибок,
    и отказ был неотличим от «в кластере нет pod'ов с PVC».
    """
    stats: Dict[str, Any] = {
        "pods_scanned": 0,
        "pods_with_pvcs": 0,
        "claim_refs_seen": 0,
        "edges_uses_volume": 0,
        "skipped_no_owner": 0,
        "skipped_no_service": 0,
        "skipped_unknown_pvc": 0,
        "errors": 0,
        "fetch_failed": False,
    }
    pods, _pods_ok = _fetch_items("pods", stats)
    # Сбой среза RS не обнуляет проход, но деградирует атрибуцию
    # (Pod → RS → Deployment не резолвится, рёбра уходят в
    # skipped_no_service) — поэтому это тоже errors, а не тишина.
    replicasets, _rs_ok = _fetch_items("replicasets", stats)
    rs_index = _build_rs_to_deployment_index(replicasets)
    stats["pods_scanned"] = len(pods)

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
        "no_owner=%d no_service=%d unknown_pvc=%d errors=%d",
        stats["pods_scanned"], stats["pods_with_pvcs"],
        stats["edges_uses_volume"], stats["skipped_no_owner"],
        stats["skipped_no_service"], stats["skipped_unknown_pvc"],
        stats["errors"],
    )
    # Отчёт для decay-guard: этот срез отвечает за свежесть `uses_volume`.
    record_source_run(SOURCE_STORAGE_PODS, stats)
    return stats


def sync_storage(db: Session) -> Dict[str, Any]:
    """Main entry: PV → PVC → pod edges → decay → disk_pct enrichment.

    Порядок важен:
      1. PV сначала — чтобы PVC.bound_to мог разрешить FK по volume_name;
      2. PVC — disk_pct прикрепляется здесь;
      3. Pod edges последними — нужны PVC-nodes в KG для (ns, name) lookup;
      4. decay рёбер — ПОСЛЕ всех срезов: он опирается на их
         `record_source_run`-отчёты, чтобы не чистить рёбра сбойного среза.

    `errors` верхнего уровня — сумма по срезам: по нему видно отказ fetch'а,
    даже если в графе «просто ничего не поменялось».
    """
    disk_pct_map: Dict[Tuple[str, str], float] = {}
    try:
        disk_pct_map = _fetch_disk_pct_map_sync()
    except Exception as e:
        logger.warning("k8s_storage.disk_pct_fetch_failed err=%s", e)

    pv_stats = sync_pvs(db)
    pvc_stats = sync_pvcs(db, disk_pct_map=disk_pct_map)
    edge_stats = sync_pod_pvc_edges(db)

    # Decay в своей транзакции: его сбой не должен обнулять уже
    # закоммиченные срезы (зеркалит Pass 3 в kg_sync.sync_topology).
    decay_error = 0
    try:
        decay_stats: Dict[str, Any] = decay_volume_edges(db)
        db.commit()
    except Exception as e:
        logger.warning("k8s_storage.volume_decay_failed err=%s", e)
        db.rollback()
        decay_error = 1
        decay_stats = {"error": str(e)}

    slices: Iterable[Dict[str, Any]] = (pv_stats, pvc_stats, edge_stats)
    errors = sum(int(s.get("errors") or 0) for s in slices) + decay_error
    result = {
        "pvs": pv_stats,
        "pvcs": pvc_stats,
        "pod_edges": edge_stats,
        "edge_decay": decay_stats,
        "errors": errors,
        "disk_pct_enabled": bool(settings.STORAGE_METRICS_ENABLED),
        "disk_pct_series": len(disk_pct_map),
    }
    if errors:
        logger.warning(
            "k8s_storage.done_with_errors errors=%d — часть срезов kubectl не "
            "получена, чистка/decay по ним пропущены (см. выше)",
            errors,
        )
    return result


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = sync_storage(db)
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
