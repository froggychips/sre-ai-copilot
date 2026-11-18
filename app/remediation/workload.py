"""Вид и имя рабочей нагрузки, на которую смотрит алерт, — без kubectl.

Зачем. Мост (`bridge.plan_proposals`) брал имя цели как
`labels.deployment or ctx.service` и сразу делал `describe deployment <имя>`.
У `CPUThrottlingHigh` лейбла `deployment` нет — есть только `pod` и
`container`, а подавляющая часть таких алертов приходит с подов StatefulSet-ов
баз (`<имя>-postgresql-0`). Playbook, умеющий только Deployment, матчился на
цель, которую исполнить не может, и на каждый алерт уходили снимок K8sFacts и
проба kubectl с исходом «live replicas unknown».

Здесь вид цели определяется из того, что уже есть, по убыванию доверия:

1. явный лейбл алерта (`deployment` / `statefulset` / `daemonset` /
   `job_name` / `cronjob`) — kube-state-metrics называет объект сам;
2. граф: workload-узел `kg_services` с именем, выведенным из имени пода
   (`metadata_json.k8s_workload.workload_kind`), либо Job из `kg_k8s_jobs`;
3. форма имени пода, когда граф объекта не знает. Workload-узлы заводятся
   только для нагрузок, на которые смотрит k8s Service, — у воркеров без
   Service узла нет, и без этого шага их вид был бы «неизвестен» всегда:
     `<имя>-<rs-hash>-<pod-hash>`  → Deployment (оба хвоста в алфавите
                                     SafeEncodeString — иначе `<ds>-<слово>-<hash>`
                                     сошёл бы за Deployment);
     `<имя>-<цифры>-<pod-hash>`    → Job от CronJob (хвост — время запуска);
     `<имя>-<ordinal>`             → StatefulSet;
4. ничего не подошло — вид неизвестен (`kind=None`). Решать, что с этим
   делать, — вызывающему; matcher для playbook-а с `match.target_kinds`
   трактует неизвестный вид как несовпадение (fail-closed).

Только SELECT, в SAVEPOINT-е: сбой запроса не оставляет транзакцию
вызывающего в aborted-состоянии.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from app.remediation.playbook import TARGET_KINDS
from app.services.alert_target import (POD_HASH_RE, POD_HASH_SAFE_RE,
                                       STS_POD_RE)

__all__ = [
    "KIND_UNKNOWN",
    "WORKLOAD_KINDS",
    "WorkloadTarget",
    "resolve_workload_target",
]

#: Виды, которые умеет назвать резолвер (и которые допустимы в
#: `match.target_kinds` playbook-а). `Cluster` — кластер CloudNativePG: так
#: его вид записан в графе.
WORKLOAD_KINDS: Tuple[str, ...] = TARGET_KINDS

#: Значение лейбла метрики и лога для неопределённого вида.
KIND_UNKNOWN = "unknown"

# Порядок как в `target_resolver._KIND_FROM_LABEL_KEYS`: конкретное впереди.
_KIND_BY_LABEL: Sequence[Tuple[str, str]] = (
    ("statefulset", "StatefulSet"),
    ("daemonset", "DaemonSet"),
    ("deployment", "Deployment"),
    ("job_name", "Job"),
    ("cronjob", "CronJob"),
)

_K8S_SAFE = "bcdfghjklmnpqrstvwxz2456789"
#: Последний сегмент имени пода: `-<ordinal>` либо `-<pod-hash>`.
_LAST_SEGMENT_RE = re.compile(rf"^(?P<owner>.+)-(?:\d+|[{_K8S_SAFE}]{{5}})$")
#: Под Job-а от CronJob: `<cronjob>-<минуты эпохи>-<pod-hash>`.
_CRONJOB_POD_RE = re.compile(r"^(?P<job>(?P<cron>.+)-\d{6,10})-[a-z0-9]{5}$")


@dataclass(frozen=True)
class WorkloadTarget:
    """Что нашли: вид (None — неизвестен), имя и откуда вывод."""

    kind: Optional[str]
    name: Optional[str]
    source: str  # alert_label | kg_workload | kg_job | pod_name | none

    @property
    def kind_label(self) -> str:
        return self.kind or KIND_UNKNOWN


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _workload_kind(meta: Any) -> Optional[str]:
    """workload_kind из metadata узла (синк кладёт его под `k8s_workload`)."""
    if not isinstance(meta, Mapping):
        return None
    nested = meta.get("k8s_workload")
    kind = (nested.get("workload_kind") if isinstance(nested, Mapping) else None) \
        or meta.get("workload_kind")
    return kind if kind in WORKLOAD_KINDS else None


def _kg_workloads(db: Any, namespace: str, names: Sequence[str]) -> List[Tuple[str, str]]:
    """[(имя, вид)] workload-узлов графа среди `names`. Ошибка — пусто."""
    if db is None or not names:
        return []
    try:
        from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service
        with db.begin_nested():
            rows = (
                db.query(Service.name, Service.metadata_json)
                .filter(
                    Service.namespace == namespace,
                    Service.name.in_(list(names)),
                    Service.node_kind == NODE_KIND_WORKLOAD,
                )
                .all()
            )
    except Exception:  # noqa: BLE001 — граф недоступен: остаётся форма имени
        return []
    out: List[Tuple[str, str]] = []
    for name, meta in rows:
        kind = _workload_kind(meta)
        if kind:
            out.append((str(name), kind))
    return out


def _kg_job_exists(db: Any, namespace: str, name: str) -> bool:
    if db is None or not name:
        return False
    try:
        from app.knowledge_graph.schema import K8sJob
        with db.begin_nested():
            return db.query(K8sJob.id).filter(
                K8sJob.namespace == namespace, K8sJob.name == name,
                K8sJob.kind == "job",
            ).first() is not None
    except Exception:  # noqa: BLE001
        return False


def _from_pod(db: Any, namespace: Optional[str], pod: str) -> WorkloadTarget:
    loose = POD_HASH_RE.match(pod)
    safe = POD_HASH_SAFE_RE.match(pod)
    last = _LAST_SEGMENT_RE.match(pod)
    cron = _CRONJOB_POD_RE.match(pod)

    candidates: List[str] = []
    for m, group in ((safe, "deploy"), (loose, "deploy"), (last, "owner")):
        if m and m.group(group) not in candidates:
            candidates.append(m.group(group))

    if namespace:
        found = _kg_workloads(db, namespace, candidates)
        if found:
            # Самое длинное имя — самое точное: `<ds>-<слово>` важнее, чем
            # `<ds>` с двумя «хэшами» в хвосте.
            name, kind = max(found, key=lambda item: len(item[0]))
            return WorkloadTarget(kind=kind, name=name, source="kg_workload")

    if cron:
        return WorkloadTarget(kind="Job", name=cron.group("job"), source="pod_name")
    if namespace and last and _kg_job_exists(db, namespace, last.group("owner")):
        return WorkloadTarget(kind="Job", name=last.group("owner"), source="kg_job")
    if safe:
        return WorkloadTarget(kind="Deployment", name=safe.group("deploy"), source="pod_name")
    sts = STS_POD_RE.match(pod)
    if sts:
        return WorkloadTarget(kind="StatefulSet", name=sts.group("sts"), source="pod_name")
    return WorkloadTarget(kind=None, name=None, source="none")


def resolve_workload_target(
    labels: Optional[Mapping[str, Any]],
    *,
    namespace: Optional[str] = None,
    service: Optional[str] = None,
    db: Any = None,
) -> WorkloadTarget:
    """Вид и имя нагрузки алерта. Без сети; с `db` — до двух SELECT-ов.

    `service` — имя, которое вывело обогащение (`EnrichedContext.service`):
    используется, только когда в лейблах нет ни явного объекта, ни пода.
    Имя в ответе может быть None при неизвестном виде — вызывающий тогда
    остаётся со своим именем.
    """
    lb: Mapping[str, Any] = labels if isinstance(labels, Mapping) else {}
    ns = _str(namespace) or _str(lb.get("namespace"))

    for key, kind in _KIND_BY_LABEL:
        value = _str(lb.get(key))
        if value:
            return WorkloadTarget(kind=kind, name=value, source="alert_label")

    pod = _str(lb.get("pod"))
    if pod:
        return _from_pod(db, ns, pod)

    name = _str(service)
    if name and ns:
        found = _kg_workloads(db, ns, [name])
        if found:
            return WorkloadTarget(kind=found[0][1], name=found[0][0], source="kg_workload")
    return WorkloadTarget(kind=None, name=name, source="none")
