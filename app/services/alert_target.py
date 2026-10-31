"""Объект алерта — k8s-target, нода или namespace, а не экспортер метрики.

ЗАЧЕМ. Метрику большинства kube-алертов собирает экспортер (kube-state-metrics,
node-exporter, vmagent, postgres/nats-exporter), и метки `service`/`job`/`pod`/
`instance` указывают на НЕГО, а не на объект, про который алерт. Еженедельная
сверка 31.10.2026: 224 из 446 алертов (50%) без атрибуции — 195 без сервиса,
29 на экспортере/synthetic. Разбор живых меток (ALERTS в VictoriaMetrics,
сопоставление по fingerprint Alertmanager-а) показал, откуда дыра:

  * CPUThrottlingHigh (~110 за неделю) — метрика cAdvisor: `pod`/`container`/
    `namespace` есть, `service`/`job` нет; store-путь отдавал такие алерты
    «прежней цепочке» `service→app→deployment` и получал пусто;
  * ScrapePoolHasNoTargets — `service=vmagent`, объект в `scrape_job`
    (`serviceScrape/<ns>/<name>/<i>`);
  * TargetDown — `job=<ns>/<name>` без `service`;
  * ProdRestartsSpike / Seq* / *ReplicaMissing — только `namespace`: объект —
    сам namespace, а не «без сервиса»;
  * нодовые алерты — объект нода, а сверка считала их «без сервиса».

ЦЕПОЧКА (`resolve_alert_target`, первое попадание выигрывает):

  0. алерт про сам экспортер (`*ExporterDown`, `TargetDown{job=<экспортер>}`)
     → экспортер, kind=exporter. Только здесь экспортер бывает целью;
  1. `deployment` → `statefulset` → `daemonset`;
  2. `cronjob` / `job_name` → владелец Job/CronJob (`job_attribution`);
  3. `horizontalpodautoscaler` / `poddisruptionbudget` → имя (== workload);
  4. обычный app-алерт (не через экспортер) с `service`/`app` → как раньше;
  5. нода: node-exporter, `Node*`/`KubeNode*`/`Kubelet*` → kind=node;
  6. `persistentvolumeclaim` → workload через `kg_volume_edges` (uses_volume);
  7. `pod` → workload (имя без хэша; через экспортер — только если workload
     есть в графе: под самого KSM — не цель);
  8. `container` + `namespace` → workload, если он есть в графе;
  9. `scrape_job` / `job=<ns>/<name>` → объект скрейпа;
 10. `service`, если это не экспортер;
 11. `node` без пода → kind=node;
 12. только `namespace` у алерта не через экспортер → kind=namespace;
 13. иначе — через экспортер: kind=unresolved (экспортер владельцем НЕ
     становится, уходит в `source`); не через экспортер — legacy_default.

Если цепочка вывела имя экспортера (KubePodCrashLooping пода nats-exporter,
KubeDaemonSetRolloutStuck{daemonset=vm-node-exporter}) — алерт про сам
экспортер, kind=exporter.

ПОТРЕБИТЕЛИ: store-путь (`auto_populator.populate_from_incident`, ключ дедупа
`/enrich-and-forward` через `resolve_store_service`) и карточка
(`alert_enrichment.enrich_alert`). Граф — через `TargetLookup`: в проде БД,
в dry-run переатрибуции — выгрузка.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple

from app.config import settings
from app.services.node_resolver import is_node_exporter_pod

# ── Экспортеры ──────────────────────────────────────────────────────────────

#: Экспортеры, чьи метки `service`/`job`/`app` — ИСТОЧНИК метрики, а не объект
#: алерта. Замер 16.10.2026 (30 дней): 168 из 592 алертов в kg_alerts лежали на
#: `vm-kube-state-metrics` и `vm-node-exporter`.
METRIC_EXPORTER_SOURCES = frozenset({
    "vm-kube-state-metrics", "kube-state-metrics",
    "vm-node-exporter", "node-exporter", "prometheus-node-exporter",
})
NODE_EXPORTER_SOURCES = frozenset({
    "vm-node-exporter", "node-exporter", "prometheus-node-exporter",
})
#: Все известные экспортеры поимённо (сверка, дайджест): источники выше +
#: vmagent.
EXPORTER_SERVICES = METRIC_EXPORTER_SOURCES | frozenset({
    "vmagent-vm-victoria-metrics-k8s-stack", "vmagent",
})
#: Остальные экспортеры — по имени: `postgres-exporter`,
#: `nats-exporter-prometheus-nats-exporter`, `*-metrics-exporter`. Тот же
#: шаблон годится для PostgreSQL `~` (сверка считает им в SQL).
EXPORTER_NAME_PATTERN = r"(^|-)exporter(-|$)|kube-state-metrics|^vmagent(-|$)"
_EXPORTER_NAME_RE = re.compile(EXPORTER_NAME_PATTERN)

NODE_ALERT_PREFIXES = ("Node", "KubeNode", "KubeletDown", "KubeletTooMany")


def is_exporter_name(name: Optional[str]) -> bool:
    """Имя сервиса/джобы — экспортер метрик."""
    name = (name or "").strip()
    if not name:
        return False
    return name in EXPORTER_SERVICES or bool(_EXPORTER_NAME_RE.search(name))


# ── Исходы ──────────────────────────────────────────────────────────────────

TARGET_SERVICE = "service"        # объект — workload/сервис
TARGET_NODE = "node"              # объект — нода, сервиса нет
TARGET_NAMESPACE = "namespace"    # объект — namespace целиком
TARGET_EXPORTER = "exporter"      # алерт про сам экспортер
TARGET_UNRESOLVED = "unresolved"  # пришёл через экспортер, цель не нашлась
TARGET_LEGACY = "legacy"          # обычный app-алерт или kill-switch

#: Исходы, при которых у алерта есть объект, хотя `service_id` пуст.
ATTRIBUTED_WITHOUT_SERVICE = frozenset({TARGET_NODE, TARGET_NAMESPACE})


@dataclass(frozen=True)
class AlertTarget:
    """Объект алерта.

    `service` — имя target-сервиса или None (алерт без сервиса, #491).
    `scope` — исход цепочки (TARGET_*), в kg_alerts уходит `raw.target_kind`.
    `source` — экспортер, через который алерт пришёл (`raw.source`), если был.
    `how` — какой шаг цепочки сработал (`raw.attribution`).
    `namespace` — namespace объекта, если он не совпадает с меткой `namespace`
    (у ScrapePoolHasNoTargets метка — ns vmagent-а, объект — в `scrape_job`);
    None — брать namespace алерта.
    `node` — нода нодового алерта.
    """
    service: Optional[str]
    scope: str
    source: Optional[str] = None
    how: str = ""
    namespace: Optional[str] = None
    node: Optional[str] = None

    @property
    def kind(self) -> str:
        return self.scope


# ── Граф ────────────────────────────────────────────────────────────────────

class TargetLookup(Protocol):
    """Что цепочке нужно от графа. БД в проде, выгрузка — в dry-run."""

    def workload_exists(self, namespace: Optional[str], name: str) -> bool: ...

    def pvc_owner(self, namespace: Optional[str], pvc: str) -> Optional[str]: ...

    def job_target(self, namespace: Optional[str], job_name: str) -> Tuple[Optional[str], str]: ...


class DbLookup:
    """Граф из kg_*; без БД — верим выводу из имён (как store-путь до графа)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def workload_exists(self, namespace: Optional[str], name: str) -> bool:
        if self.db is None:
            return True
        if not namespace:
            return False
        from app.knowledge_graph.schema import (NODE_KIND_SERVICE,
                                                NODE_KIND_WORKLOAD, Service)
        try:
            # SAVEPOINT: ошибка запроса не должна оставить транзакцию
            # store-пути в aborted-состоянии — дальше пишутся алерт и инцидент.
            with self.db.begin_nested():
                return self.db.query(Service.id).filter(
                    Service.namespace == namespace, Service.name == name,
                    Service.node_kind.in_((NODE_KIND_SERVICE, NODE_KIND_WORKLOAD)),
                ).first() is not None
        except Exception:  # noqa: BLE001 — граф недоступен: не хуже прежнего
            return True

    def pvc_owner(self, namespace: Optional[str], pvc: str) -> Optional[str]:
        if self.db is None or not namespace or not pvc:
            return None
        from app.knowledge_graph.schema import (Service, StorageVolume,
                                                VolumeEdge)
        try:
            with self.db.begin_nested():
                row = (
                    self.db.query(Service.name)
                    .join(VolumeEdge, (VolumeEdge.src_kind == "service")
                          & (VolumeEdge.src_id == Service.id))
                    .join(StorageVolume, (VolumeEdge.dst_kind == "pvc")
                          & (VolumeEdge.dst_id == StorageVolume.id))
                    .filter(
                        VolumeEdge.kind == "uses_volume",
                        StorageVolume.kind == "pvc",
                        StorageVolume.namespace == namespace,
                        StorageVolume.name == pvc,
                    )
                    .order_by(Service.name)
                    .first()
                )
        except Exception:  # noqa: BLE001
            return None
        name = row[0] if row is not None else None
        return name if isinstance(name, str) and name else None

    def job_target(self, namespace: Optional[str], job_name: str) -> Tuple[Optional[str], str]:
        from app.knowledge_graph.job_attribution import resolve_job_target
        return resolve_job_target(self.db, namespace, job_name)


# ── Имена ───────────────────────────────────────────────────────────────────

# Pod-hash strip: kube создаёт pod как `<deployment>-<rs-hash>-<pod-hash>`
# (rs-hash = 8..10 alnum, pod-hash = 5 alnum).
POD_HASH_RE = re.compile(r"^(?P<deploy>.+?)-[a-z0-9]{8,10}-[a-z0-9]{5}$")
# Короткий rs-hash (6–7 знаков: `bot-service-64f9bdf-th89b`) — только в
# алфавите SafeEncodeString k8s (без гласных и 0/1/3): иначе под Job-а
# `town-db-migrate-x9k2p` разобрался бы как deployment `town-db`.
_K8S_SAFE = "bcdfghjklmnpqrstvwxz2456789"
POD_HASH_SAFE_RE = re.compile(
    rf"^(?P<deploy>.+?)-[{_K8S_SAFE}]{{6,10}}-[{_K8S_SAFE}]{{5}}$"
)
# StatefulSet pod: `<sts>-<ordinal>` (e.g. `town-db-postgresql-0`).
STS_POD_RE = re.compile(r"^(?P<sts>.+?)-(?P<ord>\d+)$")
# Объект скрейпа vmagent: `serviceScrape/<ns>/<name>/<endpoint-index>`.
_SCRAPE_JOB_RE = re.compile(
    r"^(?:serviceScrape|podScrape|probe|staticScrape|nodeScrape)/"
    r"(?P<ns>[a-z0-9-]+)/(?P<name>[a-z0-9.-]+)(?:/\d+)?$"
)
# `job` в виде `<ns>/<name>` (VMServiceScrape без jobLabel).
_NS_JOB_RE = re.compile(r"^(?P<ns>[a-z0-9-]+)/(?P<name>[a-z0-9.-]+)$")


def strip_pod_hash(pod: str) -> Optional[str]:
    """Имя workload'а из имени пода.

    `auth-service-7f8c4b6cdf-h2x9k` → `auth-service`.
    `town-db-postgresql-0` → `town-db-postgresql` (StatefulSet ordinal).
    Не-stripped pod → None.
    """
    if not pod:
        return None
    m = POD_HASH_RE.match(pod) or POD_HASH_SAFE_RE.match(pod)
    if m:
        return m.group("deploy")
    m = STS_POD_RE.match(pod)
    if m:
        return m.group("sts")
    return None


def _job_name(value: Optional[str]) -> str:
    """`monitoring/vm-node-exporter` → `vm-node-exporter`."""
    value = (value or "").strip()
    return value.rsplit("/", 1)[-1] if "/" in value else value


def exporter_source(labels: Dict[str, str]) -> Optional[str]:
    """Имя экспортера-источника метрики или None, если алерт не от экспортера."""
    for key in ("service", "job", "app"):
        value = _job_name(labels.get(key))
        if is_exporter_name(value):
            return value
    if is_node_exporter_pod(labels.get("pod")):
        return "vm-node-exporter"
    return None


def _about_exporter_itself(labels: Dict[str, str]) -> Optional[str]:
    """Имя экспортера, если алерт про сам экспортер, а не про объект метрики."""
    alertname = labels.get("alertname") or ""
    if alertname.endswith("ExporterDown"):
        for key in ("service", "job", "app", "container"):
            value = _job_name(labels.get(key))
            if is_exporter_name(value):
                return value
        # NodeExporterDown без меток экспортера: имя из самого алерта.
        return "vm-node-exporter" if "NodeExporter" in alertname else None
    if alertname == "TargetDown":
        job = _job_name(labels.get("job"))
        if is_exporter_name(job):
            return job
    return None


def _is_node_scoped(labels: Dict[str, str], source: Optional[str]) -> bool:
    """Объект алерта — нода: node-exporter или Node*/KubeNode*/Kubelet*."""
    if source in NODE_EXPORTER_SOURCES:
        return True
    alertname = labels.get("alertname") or ""
    return alertname.startswith(NODE_ALERT_PREFIXES)


def _node_of(labels: Dict[str, str]) -> Optional[str]:
    return (labels.get("node") or "").strip() or None


# ── Цепочка ─────────────────────────────────────────────────────────────────

def resolve_alert_target(
    labels: Dict[str, str],
    *,
    legacy_default: Optional[str],
    db: Any = None,
    lookup: Optional[TargetLookup] = None,
) -> AlertTarget:
    """Объект алерта по цепочке приоритета меток (см. шапку модуля).

    `legacy_default` — прежняя fallback-цепочка вызывающего (`service→app→
    deployment`) для обычных app-алертов. kill-switch
    `ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED=False` → всегда `legacy_default`.
    """
    if not settings.ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED:
        return AlertTarget(legacy_default, TARGET_LEGACY, how="kill_switch")
    labels = labels or {}
    graph: TargetLookup = lookup if lookup is not None else DbLookup(db)
    namespace = labels.get("namespace") or None
    node = _node_of(labels)
    source = exporter_source(labels)

    def found(name: str, how: str, ns: Optional[str] = None) -> AlertTarget:
        scope = TARGET_EXPORTER if is_exporter_name(name) else TARGET_SERVICE
        return AlertTarget(name, scope, source, how, ns, node)

    # 0. Алерт про сам экспортер — единственный случай, когда он цель.
    self_exporter = _about_exporter_itself(labels)
    if self_exporter:
        return AlertTarget(self_exporter, TARGET_EXPORTER, source, "exporter_self", None, node)

    # 1. Workload прямо в метках.
    for key in ("deployment", "statefulset", "daemonset"):
        if labels.get(key):
            return found(labels[key], "workload_label")

    # 2. Job/CronJob: владелец из kg_k8s_jobs, иначе имя CronJob (#373).
    if labels.get("cronjob"):
        return found(labels["cronjob"], "cronjob")
    if labels.get("job_name"):
        target, _how = graph.job_target(namespace, labels["job_name"])
        if target:
            return found(target, "job")

    # 3. PDB/HPA: имя совпадает с workload'ом (замер 30.09.2026: 1310 из 1330
    # PDB и все 70 HPA), #472.
    for key in ("horizontalpodautoscaler", "poddisruptionbudget"):
        if labels.get(key):
            return found(labels[key], "workload_label")

    # 4. Обычный app-алерт: его `service`/`app` валиден — как раньше.
    if source is None and legacy_default:
        return AlertTarget(legacy_default, TARGET_LEGACY, None, "app_label", None, node)

    # 5. Нода: node-exporter и Node*/KubeNode* — нода не притворяется сервисом.
    if _is_node_scoped(labels, source):
        return AlertTarget(None, TARGET_NODE, source, "node", None, node)

    # 6. PVC → workload, который его монтирует (kg_volume_edges).
    pvc = labels.get("persistentvolumeclaim")
    if pvc:
        owner = graph.pvc_owner(namespace, pvc)
        if owner:
            return found(owner, "pvc")

    # 7. Под → workload. Через экспортер — только если workload есть в графе:
    # под самого экспортера (KSM мониторит себя) и удалённый под не цель. Без
    # экспортера `pod` — под самого объекта (cAdvisor), имени достаточно.
    pod = labels.get("pod")
    if pod and not is_node_exporter_pod(pod):
        derived = strip_pod_hash(pod)
        if derived and derived not in METRIC_EXPORTER_SOURCES:
            if source is None or graph.workload_exists(namespace, derived):
                return found(derived, "pod")

    # 8. Контейнер — имя workload'а у большинства чартов; только из графа.
    container = labels.get("container")
    if (
        container and namespace and not is_exporter_name(container)
        and graph.workload_exists(namespace, container)
    ):
        return found(container, "container")

    # 9. Объект скрейпа: `scrape_job` vmagent-а или `job=<ns>/<name>`.
    m = _SCRAPE_JOB_RE.match(labels.get("scrape_job") or "")
    if m:
        return found(m.group("name"), "scrape_job", m.group("ns"))
    m = _NS_JOB_RE.match(labels.get("job") or "")
    if m and not is_exporter_name(m.group("name")):
        return found(m.group("name"), "job_label", m.group("ns"))

    # 10. `service`, если это не экспортер.
    service = (labels.get("service") or "").strip()
    if service and not is_exporter_name(service):
        return found(service, "service_label")

    # 11. Нода без пода (KubeNodePressure от KSM, DevNode*).
    if node and not pod:
        return AlertTarget(None, TARGET_NODE, source, "node", None, node)

    # 12. Только namespace: объект — namespace целиком (ProdRestartsSpike,
    # Seq*, TownDbReplicaMissing). У алерта через экспортер `namespace` —
    # namespace самого экспортера, это не объект.
    if source is None and namespace and not pod and not container:
        return AlertTarget(None, TARGET_NAMESPACE, None, "namespace", None, node)

    if source is None:
        return AlertTarget(legacy_default, TARGET_LEGACY, None, "app_label", None, node)
    # 13. Через экспортер, цель не нашлась: экспортер владельцем НЕ становится.
    return AlertTarget(None, TARGET_UNRESOLVED, source, "no_target", None, node)


# ── Сверка ──────────────────────────────────────────────────────────────────

ATTR_SERVICE = "service"
ATTR_NODE = "node"
ATTR_NAMESPACE = "namespace"
ATTR_EXPORTER_SELF = "exporter_self"
ATTR_NO_SERVICE = "no_service"
ATTR_ON_EXPORTER = "on_exporter"

#: Классы строки kg_alerts, которые сверка считает «без атрибуции».
UNATTRIBUTED_CLASSES = frozenset({ATTR_NO_SERVICE, ATTR_ON_EXPORTER})


def classify_alert_row(
    service_name: Optional[str], synthetic: Optional[bool], raw: Any,
    *, real_workload: bool = False,
) -> str:
    """Класс атрибуции строки kg_alerts — для сверки и отчёта переатрибуции.

    Нода и namespace — объект есть, хоть `service_id` пуст. Экспортер —
    атрибуция, только если строка помечена как алерт про сам экспортер
    (`raw.target_kind=exporter`). Синтетический узел (обсервабилити, ушедший
    стенд) считается как раньше — «на экспортере/synthetic», кроме узла, у
    которого в графе есть живой (не synthetic) workload с тем же ns/именем
    (`real_workload`): kg_sync помечает synthetic DB-узлы `uses_db`
    (`map-db-postgresql` сквада), хотя за ними стоит настоящий StatefulSet.
    """
    raw_d = raw if isinstance(raw, dict) else {}
    kind = raw_d.get("target_kind")
    if service_name is None:
        if kind in ATTRIBUTED_WITHOUT_SERVICE:
            return ATTR_NODE if kind == TARGET_NODE else ATTR_NAMESPACE
        # Строки до target_kind: нодовые store-путь помечал attribution=node.
        if kind is None and raw_d.get("attribution") == "node":
            return ATTR_NODE
        return ATTR_NO_SERVICE
    if kind == TARGET_EXPORTER and is_exporter_name(service_name):
        return ATTR_EXPORTER_SELF
    if is_exporter_name(service_name) or (synthetic and not real_workload):
        return ATTR_ON_EXPORTER
    return ATTR_SERVICE
