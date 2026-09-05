"""Alert -> TargetRef resolver. Kubernetes-free в Phase A.

Использует только данные из:
- alert labels (namespace, pod, deployment, job, owner_kind);
- KG (`kg_services`, `kg_k8s_jobs`, `kg_pod_events`) — если передан session;
- explicit override через `facts` (dict из enrich pipeline).

Live k8s enrichment (Pod -> ReplicaSet -> Deployment via API server) — в
Phase B. Эта граница позволяет unit-test'ить decision pipeline без cluster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Импорт KG schema опционален — модуль должен работать без БД (для тестов
# без kg session). Импорт в виде Any-protocol избегает import-cycle.
try:
    from sqlalchemy.orm import Session as _SqlaSession
except ImportError:  # pragma: no cover - sqlalchemy всегда есть
    _SqlaSession = Any  # type: ignore[assignment, misc]


@dataclass
class TargetRef:
    """Чёткая идентификация k8s ресурса, на который смотрит alert.

    `resolved_via` — provenance: какие источники сложились в этот ref. Список
    из `alert_label`, `kg_service`, `kg_k8s_job`, `kg_pod_event`,
    `explicit_facts`.

    `unknown` — true если даже namespace+kind+name определить не удалось.

    `uid`/`incarnation` — идентичность самого объекта, а не его имени. Тройки
    (kind, namespace, name) хватает ровно до первого пересоздания: снесённый
    и заведённый заново Deployment носит то же имя и для всех проверок
    выглядит прежним. Между планированием действия и его исполнением проходят
    секунды, но и их достаточно — а verify после ремедиации без uid не может
    отличить «под поднялся» от «это уже другой Deployment».

    NULL означает «источник не сообщил», а не «объекта нет»: uid приходит из
    синка топологии, и узлы, заведённые алертом, его пока не имеют.
    """
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None
    owner_kind: str | None = None
    owner_name: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    replicas: int | None = None
    uid: str | None = None
    incarnation: int | None = None
    resolved_via: list[str] = field(default_factory=list)
    unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
            "owner_kind": self.owner_kind,
            "owner_name": self.owner_name,
            "labels": dict(self.labels),
            "replicas": self.replicas,
            "uid": self.uid,
            "incarnation": self.incarnation,
            "resolved_via": list(self.resolved_via),
            "unknown": self.unknown,
        }


# Известные alert label keys, по которым можно идентифицировать целевой
# ресурс. Порядок имеет значение — более конкретные впереди.
_KIND_FROM_LABEL_KEYS: Sequence[tuple[str, str]] = (
    ("statefulset", "StatefulSet"),
    ("daemonset", "DaemonSet"),
    ("deployment", "Deployment"),
    ("job_name", "Job"),
    ("cronjob", "CronJob"),
    ("pod", "Pod"),
)


def _from_alert_labels(labels: Mapping[str, Any]) -> TargetRef:
    """Достать что можно из alert labels — namespace + (deployment|pod|job)."""
    ref = TargetRef()
    if not isinstance(labels, Mapping):
        return ref

    ns = labels.get("namespace") or labels.get("ns")
    if isinstance(ns, str) and ns:
        ref.namespace = ns

    for label_key, kind_value in _KIND_FROM_LABEL_KEYS:
        v = labels.get(label_key)
        if isinstance(v, str) and v:
            ref.kind = kind_value
            ref.name = v
            ref.resolved_via.append("alert_label")
            break

    # Тонкости: иногда alert приходит с label `owner_kind`/`owner_name`
    # (наш custom enrich). Подцепляем сразу — это даёт policy axis.
    ok = labels.get("owner_kind")
    if isinstance(ok, str) and ok:
        ref.owner_kind = ok
    on = labels.get("owner_name")
    if isinstance(on, str) and on:
        ref.owner_name = on

    # Кладём оставшиеся labels (app/team_owner/component) в ref.labels —
    # пригодятся risk axes (data-plane detection).
    for k, v in labels.items():
        if isinstance(v, (str, int, float, bool)) and isinstance(k, str):
            ref.labels[k] = str(v)
    return ref


def _enrich_from_facts(ref: TargetRef, facts: Mapping[str, Any] | None) -> None:
    """Перебить ref значениями из явного facts dict (enrich override)."""
    if not facts:
        return
    target_facts = facts.get("target") if isinstance(facts, Mapping) else None
    if not isinstance(target_facts, Mapping):
        return
    used = False
    for key in ("kind", "namespace", "name", "owner_kind", "owner_name"):
        v = target_facts.get(key)
        if isinstance(v, str) and v:
            setattr(ref, key, v)
            used = True
    if "replicas" in target_facts:
        try:
            ref.replicas = int(target_facts["replicas"])
            used = True
        except (TypeError, ValueError):
            pass
    extra_labels = target_facts.get("labels")
    if isinstance(extra_labels, Mapping):
        for k, v in extra_labels.items():
            if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                ref.labels[k] = str(v)
        used = True
    if used and "explicit_facts" not in ref.resolved_via:
        ref.resolved_via.append("explicit_facts")


def _enrich_from_kg(ref: TargetRef, kg_session: Any) -> None:
    """Используем KG, чтобы заполнить owner_* для Job/Pod.

    Только SELECT-операции. Phase A — read-only.
    """
    if kg_session is None or not ref.namespace:
        return
    # Lazy import — позволяет тестам подсовывать mock без перетаскивания
    # всего KG schema.
    try:
        from app.knowledge_graph.schema import (NODE_KIND_SERVICE,
                                                NODE_KIND_WORKLOAD, K8sJob,
                                                PodEvent, Service)
    except Exception:  # pragma: no cover - safety net
        return

    # 1) Если kind=Job и есть name — взять owner_kind из kg_k8s_jobs.
    if ref.kind == "Job" and ref.name:
        row = (
            kg_session.query(K8sJob)
            .filter(K8sJob.namespace == ref.namespace, K8sJob.name == ref.name)
            .first()
        )
        if row is not None:
            meta = row.metadata_json or {}
            # owner может быть в metadata_json (см. k8s_jobs_sync) либо
            # в owner_service_name (label-attribution). CronJob ref'ит
            # owner_kind/name напрямую.
            if isinstance(meta, dict):
                owner_kind = meta.get("owner_kind")
                owner_name = meta.get("owner_name")
                if isinstance(owner_kind, str) and owner_kind and not ref.owner_kind:
                    ref.owner_kind = owner_kind
                if isinstance(owner_name, str) and owner_name and not ref.owner_name:
                    ref.owner_name = owner_name
            # active_count/failed_count для freshness/blast — кладём в labels
            # как `_kg_active_jobs` / `_kg_failed_jobs` (префикс защищает от
            # collision с k8s labels).
            if row.active_count is not None:
                ref.labels.setdefault("_kg_active_jobs", str(row.active_count))
            if row.failed_count is not None:
                ref.labels.setdefault("_kg_failed_jobs", str(row.failed_count))
            ref.resolved_via.append("kg_k8s_job")

    # 2) Если kind=Pod и есть name — посмотреть в kg_pod_events и связать
    # с kg_services по service_id.
    if ref.kind == "Pod" and ref.name:
        ev = (
            kg_session.query(PodEvent)
            .filter(
                PodEvent.namespace == ref.namespace,
                PodEvent.pod_name == ref.name,
            )
            .order_by(PodEvent.first_seen.desc())
            .first()
        )
        if ev is not None and ev.service_id:
            svc = kg_session.get(Service, ev.service_id)
            if svc is not None:
                if not ref.owner_name:
                    ref.owner_name = svc.name
                # owner_kind для pod, ассоциированного с Service в KG, —
                # обычно Deployment (в WO StatefulSet тоже бывает, но KG
                # не различает). Hint=Deployment как наиболее вероятное.
                if not ref.owner_kind:
                    ref.owner_kind = "Deployment"
                ref.resolved_via.append("kg_pod_event")

    # 3) Если kind/name тот же, что у Service (Deployment), — подцепить
    # team_owner/labels.
    if ref.name and ref.kind in ("Deployment", "StatefulSet"):
        svc = (
            kg_session.query(Service)
            .filter(
                Service.namespace == ref.namespace,
                Service.name == ref.name,
                Service.node_kind == NODE_KIND_SERVICE,
            )
            .first()
        )
        if svc is not None:
            if svc.team_owner and "team_owner" not in ref.labels:
                ref.labels["team_owner"] = svc.team_owner
            meta = svc.metadata_json or {}
            if isinstance(meta, dict):
                replicas = meta.get("replicas")
                if replicas is not None and ref.replicas is None:
                    try:
                        ref.replicas = int(replicas)
                    except (TypeError, ValueError):
                        pass
            ref.resolved_via.append("kg_service")

    # 4) Идентичность объекта берём с workload-узла: у k8s Service выше свой
    # uid, и подставлять его как uid Deployment'а — та же подмена, ради
    # которой узлы и разделили на два node_kind.
    if ref.name and ref.kind in ("Deployment", "StatefulSet", "DaemonSet"):
        workload = (
            kg_session.query(Service)
            .filter(
                Service.namespace == ref.namespace,
                Service.name == ref.name,
                Service.node_kind == NODE_KIND_WORKLOAD,
            )
            .first()
        )
        if workload is not None and workload.k8s_uid:
            ref.uid = str(workload.k8s_uid)
            ref.incarnation = int(workload.incarnation or 1)
            ref.resolved_via.append("kg_workload_uid")


def resolve_target(
    alert: Mapping[str, Any],
    facts: Mapping[str, Any] | None = None,
    kg_session: Any = None,
) -> TargetRef:
    """Собрать TargetRef из (alert, facts, kg).

    Layering (по убыванию приоритета):
        1. `facts.target.*` (explicit override) — победитель.
        2. `alert.labels` — основной источник kind/ns/name.
        3. KG enrichment — добавляет owner_kind/owner_name/replicas/labels.

    Если результат не имеет ns/kind/name — TargetRef.unknown=True.
    Это поведение требуется policy: unknown target -> block.
    """
    labels = (alert or {}).get("labels") or {}
    if not isinstance(labels, Mapping):
        labels = {}

    ref = _from_alert_labels(labels)
    _enrich_from_facts(ref, facts)
    _enrich_from_kg(ref, kg_session)

    if not (ref.namespace and ref.kind and ref.name):
        ref.unknown = True
    # Удалить дубликаты в resolved_via, сохранив порядок.
    seen: set[str] = set()
    deduped: list[str] = []
    for source in ref.resolved_via:
        if source not in seen:
            deduped.append(source)
            seen.add(source)
    ref.resolved_via = deduped
    return ref
