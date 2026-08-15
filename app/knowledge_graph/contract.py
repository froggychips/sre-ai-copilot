"""KG schema / quality contract — формальный реестр инвариантов графа.

Этот модуль — **единственный источник истины** про то, что в Knowledge
Graph считается service / orphan / synthetic / owner-known, и какие
edge kinds допустимы. Все consumer'ы (stats_digest, health_score,
quality dashboards, новые wave'ы) должны импортировать константы и
утилиты отсюда, а не дублировать литералы.

Структура:
  * `KG_SCHEMA_VERSION` — текущая версия контракта (major.minor).
  * `EDGE_KINDS` — реестр всех edge kinds с семантикой и источником.
  * `SERVICE_KINDS` / `SYNTHETIC_KINDS` — допустимые `kind`/synthetic-prefix'ы.
  * `OWNER_SOURCES` — откуда берётся `team_owner`.
  * Утилиты: `is_synthetic`, `is_orphan`, `service_kind_of`,
    `owner_known`, `STARTUP_CONTRACT_CHECK`.

См. также `docs/KG_SCHEMA_CONTRACT.md` — человеко-читаемая версия с
quality metrics и compatibility policy.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Set, TypedDict

if TYPE_CHECKING:  # pragma: no cover - only for typing
    from sqlalchemy.orm import Session

    from app.knowledge_graph.schema import Service


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

#: KG schema contract version. Bump rules — см. docs/KG_SCHEMA_CONTRACT.md.
#: 2.7 = db-узлы из secret_hint привязаны к realm (`<realm>-shared`) вместо
#: схлопывания по имени. До этого `db:postgres:config` был ОДНИМ узлом в
#: `preprod-kingdom1` с 1430 рёбрами из 106 namespace — при 41 физической базе.
#: Граф отвечал, что прод ходит в базу препрода, причём в blast radius.
#: Breaking для консьюмеров, считавших такой узел одной сущностью; рёбра на
#: старый узел уйдут сами через edge_decay.
#: 2.6 = `kg_services.owner_source` (миграция 20260814_0100): провенанс
#: владельца перестал быть неразличимым. Бамп версии при этом забыли — в коде
#: на 2.6 ссылались комментарии, а константа оставалась 2.5 до 15.08.2026.
#: 2.1 = после Wave 7 (PodEvent corr / Service+Ingress topology / NATS subjects).
#: 2.2 = после PR #82 (k8s_jobs_sync), #84 (k8s_storage_sync) и #86
#: (kg_services.stale_class column + multi-signal owner inference).
#: 2.5 = orphan перестал засчитывать `serves_traffic` как связность: это
#: ребро на собственный workload, оно появилось у всех разом вместе с
#: node_kind и занизило orphan с 72.5% до 42% без единой новой интеграции.
#: 2.4 = `kg_services.node_kind`: узел графа перестал означать одновременно
#: k8s Service и workload. serves_traffic снова строится (было 3 ребра на весь
#: граф), метрики качества (orphan/owner/сервисов всего) считаются по
#: node_kind='service'. Additive: старые строки получают 'service' миграцией.
#: 2.3 = orphan-метрика переведена на app-scope (знаменатель — real-сервисы
#: без expected_stale-инфры) + единый источник `compute_orphan_stats`; все
#: consumer'ы (STARTUP_CONTRACT_CHECK, quality_report, stats_digest) считают
#: orphan через него. EDGE_KINDS не менялись.
KG_SCHEMA_VERSION: str = "2.7"


# ---------------------------------------------------------------------------
# Service / synthetic kinds
# ---------------------------------------------------------------------------

#: Реальные сервисы (k8s workload-у соответствует pod).
REAL_SERVICE_KINDS: Set[str] = {
    "deployment",
    "statefulset",
    "daemonset",
}

#: Synthetic-узлы — есть в kg_services, но pod-а за ними нет. Создаются
#: парсерами/sync-ами как «якоря зависимостей»: ingress hostname, NATS
#: subject, синтетическая БД-узел и т.п. Исключаются из orphan-метрики
#: (по дизайну без edges были бы).
SYNTHETIC_KINDS: Set[str] = {
    "ingress",      # k8s_ingress_sync — external entrypoint, name=`ingress:<host>`
    "subject",      # nats_subjects_sync (Wave 7-Z) — name=`subject:<value>`
    "db",           # kg_sync — synthetic `db:<driver>:<host>` для uses_db edge
    "nats",         # kg_sync — кластер NATS как target uses_nats edge
}

#: Полный набор kinds которые могут быть у Service. Используется
#: контрактом для валидации новых типов.
SERVICE_KINDS: Set[str] = REAL_SERVICE_KINDS | SYNTHETIC_KINDS


#: Роль узла в графе (`kg_services.node_kind`). НЕ то же, что `kind`: `kind`
#: описывает k8s-тип ресурса (deployment/statefulset/ingress), а `node_kind`
#: отвечает на вопрос «что этот узел означает».
#:
#: Разделение введено потому, что один тип узла означал сразу две сущности:
#: k8s Service `auth` и Deployment `auth` схлопывались в одну строку
#: kg_services (ключ был namespace+name). Следствие — ребро serves_traffic
#: (Service → backing workload) физически не могло существовать: оно всегда
#: получалось self-loop. На живом графе 07.08.2026 так терялось 2092 ребра за
#: тик, а в графе оставалось 3 ребра serves_traffic.
#:
#: Метрики качества графа (orphan, owner-coverage, «сервисов всего») считаются
#: ТОЛЬКО по node_kind='service' — иначе workload-узлы удваивают знаменатель.
#: Ребро Service → backing workload. Вынесено в константу, потому что
#: orphan-метрика обязана его игнорировать (см. compute_orphan_stats).
EDGE_SERVES_TRAFFIC: str = "serves_traffic"

NODE_KIND_SERVICE: str = "service"
NODE_KIND_WORKLOAD: str = "workload"
NODE_KIND_INGRESS: str = "ingress"
NODE_KINDS: Set[str] = {NODE_KIND_SERVICE, NODE_KIND_WORKLOAD, NODE_KIND_INGRESS}

#: Имя UNIQUE-констрейнта узла графа — (namespace, name, node_kind).
#: Литерал этого имени раньше жил в ДВУХ независимых `ON CONFLICT` (populator и
#: kg_sync). При переходе на трёхколоночный ключ (миграция 20260807_0200) вторую
#: копию пропустили: `ON CONFLICT` сослался на удалённый констрейнт, и
#: kg_topology_sync падал на КАЖДОМ namespace — 79 ошибок за тик, services=0,
#: граф по сервисам не обновлялся сутки (#245). Имя объявляется здесь, а
#: `schema.py` и единственный upsert берут его отсюда: разъехаться больше нечему.
UQ_KG_SERVICE_NS_NAME_KIND: str = "uq_kg_service_ns_name_kind"

#: Node kinds для `kg_volume_edges` (PR #84, k8s_storage_sync). Это
#: heterogeneous-граф: src/dst могут быть из `kg_services` ИЛИ из
#: `kg_storage_volumes`. Поэтому отдельный namespacing от SERVICE_KINDS.
#:   * `service` — обычный сервис из kg_services (любой REAL_SERVICE_KIND);
#:   * `pvc`/`pv` — узлы из kg_storage_volumes.
STORAGE_NODE_KINDS: Set[str] = {"service", "pvc", "pv"}


# ---------------------------------------------------------------------------
# Owner sources
# ---------------------------------------------------------------------------

#: Откуда взят `kg_services.team_owner`. С contract 2.6 (миграция
#: 20260814_0100) это не только семантика, но и колонка `owner_source`:
#: до неё 12 577 узлов с владельцем выглядели одинаково надёжными, хотя
#: угаданный по префиксу namespace владелец и владелец из k8s-лейбла — вещи
#: очень разного качества. NULL в колонке = провенанс неизвестен.
#:
#: Naming convention: контракт фиксирует «slug» источника (snake_case,
#: длинная форма). `ownership_suggester` использует короткие алиасы в поле
#: `OwnerSuggestion.sources` ради компактности; они должны мапиться сюда:
#:   suggester    ↔ contract
#:   prefix       ↔ namespace_prefix
#:   labels       ↔ k8s_labels
#:   deploy_history ↔ deploy_history
#:   manual       ↔ manual
#: Именованные константы источников. Объявлены, чтобы продюсеры не писали
#: литералы: строка `"namespace_prefix"`, разъехавшаяся между таблицей и
#: писателем, уже стоила графу 7209 занижённых рёбер в соседнем реестре
#: (`confidence._SOURCE_PRECEDENCE`, исправлено 15.08.2026).
OWNER_SOURCE_MANUAL: str = "manual"
OWNER_SOURCE_K8S_LABELS: str = "k8s_labels"
OWNER_SOURCE_NAMESPACE_PREFIX: str = "namespace_prefix"
OWNER_SOURCE_DEPLOY_HISTORY: str = "deploy_history"
OWNER_SOURCE_PLATFORM_STATIC: str = "platform_static"
OWNER_SOURCE_SUGGESTED: str = "suggested"

OWNER_SOURCES: Set[str] = {
    OWNER_SOURCE_MANUAL,            # ручная правка через admin endpoint / OWNERSHIP_MANIFEST_PATH
    OWNER_SOURCE_K8S_LABELS,        # лейбл `team-owner` / `owner` / `squad` / part-of (alias: `labels`)
    OWNER_SOURCE_NAMESPACE_PREFIX,  # эвристика по namespace (`squad-N` → owner=`squad-N`) (alias: `prefix`)
    OWNER_SOURCE_DEPLOY_HISTORY,    # PR #85: most-frequent `triggered_by` за 30d в kg_deployments
    OWNER_SOURCE_PLATFORM_STATIC,   # synthetic-узлы platform/data/external — захардкожено
    OWNER_SOURCE_SUGGESTED,         # AI/heuristic suggestion (требует approve, planned)
}

#: Маппинг коротких алиасов из `OwnerSuggestion.sources` в канонические
#: ключи `OWNER_SOURCES`. Используется тестами `tests/test_contract_drift.py`
#: и (optionally) дашбордами которые хотят показывать длинные имена.
OWNER_SOURCE_ALIASES: Dict[str, str] = {
    "prefix": "namespace_prefix",
    "labels": "k8s_labels",
    "deploy_history": "deploy_history",
    "manual": "manual",
}

#: Насколько доверять владельцу из данного источника (1.0 — максимум).
#: Не порог и не фильтр, а подсказка потребителю: на чей `team_owner` можно
#: ссылаться в эскалации, а какой стоит перепроверить, прежде чем звать людей
#: ночью. Префиксная эвристика первой ломается на переименованиях сквадов,
#: лейбл ставит человек, «suggested» ещё никем не подтверждён.
OWNER_SOURCE_TRUST: Dict[str, float] = {
    "manual": 1.0,
    "k8s_labels": 0.9,
    "platform_static": 0.8,
    "deploy_history": 0.6,
    "namespace_prefix": 0.4,
    "suggested": 0.2,
}


def owner_source_valid(source: Optional[str]) -> bool:
    """Известен ли источник владельца. None (провенанс не указан) — валиден."""
    return source is None or source in OWNER_SOURCES


# ---------------------------------------------------------------------------
# Edge kinds inventory
# ---------------------------------------------------------------------------

class EdgeKindSpec(TypedDict):
    """Описание одного edge kind."""
    semantic: str          # что edge значит человеческими словами
    src_kinds: Set[str]    # допустимые kinds у src
    dst_kinds: Set[str]    # допустимые kinds у dst
    source: str            # какой sync/parser создаёт edge
    example: str           # пример (для документации)
    status: str            # "active" | "planned"
    table: str             # где edge живёт: 'kg_service_edges' |
                           #   'kg_volume_edges' | 'fk_only' (через FK,
                           #   а не отдельный edge-row) | 'metadata_only'
                           #   (через owner_service_id metadata column).


#: Реестр edge kinds. Любой новый kind должен:
#:   1. Появиться здесь с status='planned' до merge wave-а.
#:   2. Переключиться в 'active' одновременно с merge.
#:   3. Бампнуть KG_SCHEMA_VERSION (см. compatibility policy).
EDGE_KINDS: Dict[str, EdgeKindSpec] = {
    "calls": {
        "semantic": "Синхронный HTTP/gRPC вызов src → dst",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS | {"ingress"},
        "source": "kg_sync (env_url_v2 / env_vars / ingress)",
        "example": "town-service --calls--> world-service",
        "status": "active",
        "table": "kg_service_edges",
    },
    "uses_db": {
        "semantic": "src читает/пишет в БД (synthetic db-узел)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": {"db"},
        "source": "kg_sync (env_vars: *_CONN / *_DSN)",
        "example": "town-service --uses_db--> db:postgres:postgres-squad-1",
        "status": "active",
        "table": "kg_service_edges",
    },
    "uses_nats": {
        "semantic": "src подключается к NATS-кластеру",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": {"nats", "subject"},
        "source": "kg_sync (nats_env) + nats_subjects_sync (Wave 7-Z)",
        "example": "town-service --uses_nats--> subject:march-export",
        "status": "active",
        "table": "kg_service_edges",
    },
    "serves_traffic": {
        "semantic": "k8s Service src маршрутизирует трафик на свой backing "
                    "workload dst (selector-match)",
        # NB: src/dst живут в node_kind namespace (contract 2.4), не в
        # SERVICE_KINDS: это ребро связывает роль-узлы одной пары
        # «Service + workload», а не k8s-типы. До этой правки registry
        # описывал противоположное направление («src получает трафик через
        # ingress dst», dst_kinds={'ingress'}) — producer же НИКОГДА не писал
        # рёбра на ingress-узлы; consumer'ы, поверившие «источнику истины»
        # (blast_radius_for), искали dst=Service-узел и не матчили ничего.
        "src_kinds": {NODE_KIND_SERVICE},
        "dst_kinds": {NODE_KIND_WORKLOAD},
        "source": "k8s_topology_resources_sync (Wave 7 / G1.3)",
        "example": "wo-api-squad-1 (Service-узел) --serves_traffic--> wo-api-squad-1 (workload-узел, Deployment)",
        "status": "active",
        "table": "kg_service_edges",
    },
    "routes_to": {
        "semantic": "ingress правило роутит на backend service",
        "src_kinds": {"ingress"},
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "k8s_topology_resources_sync (Wave 7 / G1.3)",
        "example": "ingress:wo-api-squad-1.* --routes_to--> wo-api-squad-1",
        "status": "active",
        "table": "kg_service_edges",
    },
    "pod_event_of": {
        "semantic": "Pod event (OOMKilled/CrashLoop) принадлежит сервису",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "runtime_correlation (Wave 7-Y)",
        "example": "kg_pod_events row linked → kg_services row (через service_id FK)",
        "status": "active",
        # Не отдельный edge-row в kg_service_edges. Связь — через
        # `kg_pod_events.service_id` FK на kg_services.id. Запись здесь
        # ради semantic-инвентаризации (graph queries должны понимать,
        # что pod events ↔ services связаны).
        "table": "fk_only",
    },
    # ---- Promoted from planned → active (PR #82, #84) ----
    "runs_as_job": {
        "semantic": "Service запускается как k8s Job/CronJob (не Deployment)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "k8s_jobs_sync (PR #82)",
        "example": "backup-cron-town --runs_as_job--> owner Service (через K8sJob.owner_service_id)",
        "status": "active",
        # Реализовано без отдельного edge-row: `K8sJob.owner_service_id`
        # metadata-column в `kg_k8s_jobs`. Compromise: ради одного
        # edge-типа отдельный poly-graph не оправдан.
        "table": "metadata_only",
    },
    "uses_volume": {
        "semantic": "Service монтирует PVC (storage dependency)",
        # NB: src/dst живут в STORAGE_NODE_KINDS namespace (`service`/`pvc`),
        # не в SERVICE_KINDS — потому что dst — узел из kg_storage_volumes,
        # не из kg_services. validation утилиты должны это учитывать.
        "src_kinds": {"service"},
        "dst_kinds": {"pvc"},
        "source": "k8s_storage_sync.sync_pod_pvc_edges (PR #84)",
        "example": "postgres-squad-1 --uses_volume--> pvc:data-postgres-squad-1-0",
        "status": "active",
        "table": "kg_volume_edges",
    },
    "bound_to": {
        "semantic": "PVC связан с конкретным PV (для cluster-PV резерва)",
        "src_kinds": {"pvc"},
        "dst_kinds": {"pv"},
        "source": "k8s_storage_sync.sync_pvcs (PR #84)",
        "example": "pvc:data-postgres-squad-1-0 --bound_to--> pv:pvc-abc123",
        "status": "active",
        "table": "kg_volume_edges",
    },
}


# ---------------------------------------------------------------------------
# Quality thresholds (используются dashboards + alert-генератором)
# ---------------------------------------------------------------------------

#: Что считается «good» KG. Превышение этих границ — warning.
QUALITY_THRESHOLDS: Dict[str, float] = {
    "orphan_rate_max_pct": 10.0,      # orphan / real_services × 100
    "owner_coverage_min_pct": 90.0,   # услуг с team_owner != None
    "sha_coverage_min_pct": 50.0,     # deploys с sha != None
    "deploy_attribution_min_pct": 50.0,  # services с >= 1 deploy за 30d
    "synthetic_share_max_pct": 40.0,  # synthetic / total — пороже только для алертов
}


#: Окно после которого сервис без deploy считается «не атрибуцирован».
DEPLOY_ATTRIBUTION_WINDOW_DAYS: int = 30

#: Окно после которого edge без last_seen_at считается stale.
EDGE_STALE_WINDOW_DAYS: int = 7


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def is_synthetic(service: "Service") -> bool:
    """Synthetic-узел: создан парсером, реальный pod за ним не стоит.

    Источник истины — `kg_services.synthetic` колонка. Эвристика по
    префиксу имени (`ingress:` / `subject:` / `db:`) оставлена как
    fallback на случай если в legacy-данных колонка не выставлена.
    """
    if getattr(service, "synthetic", False):
        return True
    name: Optional[str] = getattr(service, "name", None)
    if not name:
        return False
    for prefix in SYNTHETIC_KINDS:
        if name.startswith(f"{prefix}:"):
            return True
    return False


def service_kind_of(service: "Service") -> str:
    """Вернуть kind сервиса: `ingress` / `subject` / `db` / `nats` /
    `deployment` (для остальных — default).

    metadata_json может содержать `workload_kind` (planned to be set by
    syncs) — используем его если есть, иначе эвристика по имени.
    """
    name: Optional[str] = getattr(service, "name", None)
    if name:
        for prefix in SYNTHETIC_KINDS:
            if name.startswith(f"{prefix}:"):
                return prefix
    meta = getattr(service, "metadata_json", None) or {}
    workload = meta.get("workload_kind") if isinstance(meta, dict) else None
    if workload in REAL_SERVICE_KINDS:
        return workload
    return "deployment"


def is_orphan(
    service: "Service",
    edge_ids_seen: Iterable[int],
    *,
    has_recent_deploy: bool = False,
    is_expected_stale: bool = False,
) -> bool:
    """Orphan service по **каноническому** правилу (v2.3):

      * НЕ synthetic, И
      * нет ни одной edge'и ЛЮБОГО kind (как src ИЛИ dst), И
      * stale_class != 'expected_stale' (инфра — DB/headless/system —
        безрёберна by design, исключается).

    WO общается через NATS/Orleans/БД, а не только HTTP — поэтому учитываем
    edge ЛЮБОГО kind, не только HTTP. expected_stale-узлы edge-less by design
    и не должны загрязнять метрику.

    `edge_ids_seen` — итерируемое со всеми service_id, которые
    встречаются как src или dst в kg_service_edges (caller получает
    через `SELECT DISTINCT src_id UNION DISTINCT dst_id`).

    Параметр `is_expected_stale` опциональный: если True — сервис не
    считается orphan (он инфра по дизайну без рёбер). Caller, не имеющий
    stale_class под рукой, передаёт default=False.

    Параметр `has_recent_deploy` сохранён для backward-compat: если True —
    сервис не orphan. Caller'ы которые его не передают → поведение без
    изменений.
    """
    if is_synthetic(service):
        return False
    if is_expected_stale:
        return False
    svc_id = getattr(service, "id", None)
    if svc_id is None:
        return False
    if svc_id in set(edge_ids_seen):
        return False
    if has_recent_deploy:
        return False
    return True


class OrphanStats(TypedDict):
    """Результат `compute_orphan_stats`."""
    orphan: int
    app_scope: int
    orphan_pct: Optional[float]


def compute_orphan_stats(db: "Session") -> OrphanStats:
    """**Единственный источник** orphan-метрики (v2.3).

    Считает в SQL то же, что per-service сумма `is_orphan(...)`:

      * `app_scope` = count real (NOT synthetic) **service**-узлов с
        `coalesce(stale_class,'') <> 'expected_stale'`. workload-узлы в
        знаменатель не входят: у них ребро serves_traffic есть всегда, и
        включение их в scope занизило бы orphan_pct вдвое без единого
        реально починенного сервиса.
      * `orphan` = из них те, чей id НЕ встречается ни как src, ни как dst
        ни в одной edge (`kg_service_edges`), КРОМЕ `serves_traffic`.
        **Self-loop'ы (src_id == dst_id) НЕ считаются связностью**: сервис,
        соединённый только сам с собой, топологически изолирован = orphan.
        (До #190 serves_traffic-петли маскировали реальных orphan'ов как
        non-orphan; этот guard не даёт маске вернуться.)

        Почему `serves_traffic` исключён (contract 2.5): это ребро Service →
        его собственный backing workload, то есть связь узла со своей же
        реализацией, а не с другим сервисом. Пока типа узла не было, такое
        ребро вырождалось в self-loop и отбрасывалось; с node_kind оно стало
        настоящим — и разом «вылечило» 1506 orphan'ов, ничего не изменив в
        интеграциях. Замер 08.08.2026: 72.5% → 42.0% при неизменной
        межсервисной связности (3578 orphan'ов и до, и после). Метрика не
        должна улучшаться от того, что мы поменяли схему хранения.
      * `orphan_pct` = round(100*orphan/app_scope, 1), или None если
        app_scope == 0 (пустая БД — не показываем ложный 0%).

    Read-only. Все consumer'ы (STARTUP_CONTRACT_CHECK / quality_report /
    stats_digest) обязаны звать именно это, а не дублировать SQL.
    """
    from sqlalchemy import text

    # Значение expected_stale — bound param (:exp), не конкатенация в SQL
    # (Bandit B608 + чистая практика; значение и так доверенная константа).
    params = {"exp": STALE_CLASS_EXPECTED_STALE}
    params["svc_kind"] = NODE_KIND_SERVICE
    params["st"] = EDGE_SERVES_TRAFFIC
    app_scope = db.execute(text(
        "SELECT count(*) FROM kg_services s "
        "WHERE NOT s.synthetic "
        "  AND s.node_kind = :svc_kind "
        "  AND coalesce(s.stale_class, '') <> :exp"
    ), params).scalar() or 0
    orphan = db.execute(text(
        "SELECT count(*) FROM kg_services s "
        "WHERE NOT s.synthetic "
        "  AND s.node_kind = :svc_kind "
        "  AND coalesce(s.stale_class, '') <> :exp "
        "  AND s.id NOT IN ("
        "      SELECT src_id FROM kg_service_edges "
        "      WHERE src_id <> dst_id AND kind <> :st "
        "      UNION SELECT dst_id FROM kg_service_edges "
        "      WHERE src_id <> dst_id AND kind <> :st"
        "  )"
    ), params).scalar() or 0
    orphan_pct = round(100.0 * orphan / app_scope, 1) if app_scope > 0 else None
    return {"orphan": int(orphan), "app_scope": int(app_scope), "orphan_pct": orphan_pct}


#: Как namespace относится к среде. Порядок важен: первое совпадение выигрывает,
#: поэтому `preupdate-` и `preprod-` стоят до общего правила.
ENV_PREFIXES: tuple[tuple[str, str], ...] = (
    ("prod-", "prod"),
    ("preprod-", "preprod"),
    ("preupdate-", "preupdate"),
    ("squad-", "squad"),
)
ENV_OTHER: str = "infra/other"


#: Хвосты namespace, за которыми стоит «этот realm, но другая роль». Данные
#: кластера на 15.08.2026: `prod-kingdom1`, `preprod-qa-kingdom2`,
#: `squad-37-kingdom2` — у всех БД лежит в `<realm>-shared`.
_REALM_TAIL_RE = re.compile(r"-(?:kingdom\d+|shared)$")

#: Имя namespace, где живут БД realm'а.
SHARED_SUFFIX: str = "-shared"


def shared_namespace_of(namespace: Optional[str]) -> Optional[str]:
    """`<realm>-shared` для namespace, или None если правило не применимо.

        prod-kingdom1        → prod-shared
        preprod-qa-kingdom2  → preprod-qa-shared
        squad-37-kingdom2    → squad-37-shared
        prod-shared          → prod-shared      (сам себе)
        sre-ai               → None             (не realm-namespace)

    Зачем. `db:<driver>:<host>`-узлы из secret_hint строятся эвристикой, и
    раньше дедуп сводил их в ОДИН узел на весь кластер — «канонический»
    выбирался лексикографическим минимумом среди уже существующих. Замер
    15.08.2026: `db:postgres:config` жил в `preprod-kingdom1` и собрал 1430
    рёбер из 106 namespace, тогда как физически таких баз 41 — по одной в
    каждом `*-shared`. Граф утверждал, что `prod-kingdom1` ходит в базу
    препрода: не потеря точности, а неверный факт о проде, причём ровно в
    том запросе, ради которого граф существует (blast radius).

    Правило выведено из кластера, а не угадано, но применять его вслепую всё
    равно нельзя — вызывающий код обязан проверить, что такой namespace
    существует (см. `kg_sync._db_namespace_for_client`). Неизвестное имя
    лучше оставить в own_namespace, чем сослаться на выдуманный.
    """
    ns = (namespace or "").strip().lower()
    if not ns:
        return None
    if ns.endswith(SHARED_SUFFIX):
        return ns
    realm = _REALM_TAIL_RE.sub("", ns)
    if realm == ns:
        # Хвост не распознан: `sre-ai`, `monitoring`, `prod-lo-legal`.
        # Придумывать им shared-пару не на чем.
        return None
    return f"{realm}{SHARED_SUFFIX}"


def env_of_namespace(namespace: Optional[str]) -> str:
    """Среда по имени namespace. Неизвестное → `infra/other`."""
    ns = (namespace or "").lower()
    for prefix, env in ENV_PREFIXES:
        if ns.startswith(prefix):
            return env
    return ENV_OTHER


def compute_orphan_stats_by_env(db: "Session") -> Dict[str, OrphanStats]:
    """Тот же orphan, но разрезанный по средам.

    Зачем разрез: агрегат по всему графу вводит в заблуждение, потому что
    подавляющая часть узлов — эфемерные dev-сквады. Замер 14.08.2026:

        squad        4447 узлов, 61.7% orphan
        preupdate     191            57.1%
        preprod       141            52.5%
        prod          160            13.8%
        infra/other    32            93.8%

    Общая цифра при этом 59.9% — то есть она почти целиком описывает связность
    стендов, живущих несколько дней, и прячет главное: на проде, где реально
    нужен blast radius, orphan уже 13.8%. Цель «снизить orphan» имеет смысл
    только per-env, иначе она превращается в задачу «дорисовать рёбра сквадам».

    Считаем в Python поверх одного прохода по узлам: сред пять, узлов тысячи —
    пять отдельных SQL-агрегатов дороже и не атомарны между собой.
    """
    from sqlalchemy import text

    params = {
        "exp": STALE_CLASS_EXPECTED_STALE,
        "svc_kind": NODE_KIND_SERVICE,
        "st": EDGE_SERVES_TRAFFIC,
    }
    rows = db.execute(text(
        "SELECT s.namespace, "
        "       (s.id NOT IN ("
        "           SELECT src_id FROM kg_service_edges "
        "           WHERE src_id <> dst_id AND kind <> :st "
        "           UNION SELECT dst_id FROM kg_service_edges "
        "           WHERE src_id <> dst_id AND kind <> :st"
        "       )) AS is_orphan "
        "FROM kg_services s "
        "WHERE NOT s.synthetic "
        "  AND s.node_kind = :svc_kind "
        "  AND coalesce(s.stale_class, '') <> :exp"
    ), params).fetchall()

    buckets: Dict[str, OrphanStats] = {}
    for namespace, is_orphan in rows:
        env = env_of_namespace(namespace)
        slot = buckets.setdefault(
            env, {"orphan": 0, "app_scope": 0, "orphan_pct": None}
        )
        slot["app_scope"] += 1
        if is_orphan:
            slot["orphan"] += 1
    for slot in buckets.values():
        if slot["app_scope"]:
            slot["orphan_pct"] = round(100.0 * slot["orphan"] / slot["app_scope"], 1)
    return buckets


def owner_known(service: "Service") -> bool:
    """Заполнен ли `team_owner` (не None, не пустая строка, не 'unknown')."""
    owner: Optional[str] = getattr(service, "team_owner", None)
    if not owner:
        return False
    if owner.strip().lower() in {"unknown", "n/a", "-", "none"}:
        return False
    return True


#: Все известные node-kinds (service + storage + графовые NODE_KINDS из
#: contract 2.4 — serves_traffic описан в терминах node_kind, см. NB там).
#: Используется тестами drift-check для валидации src/dst у edge-specs.
ALL_NODE_KINDS: Set[str] = SERVICE_KINDS | STORAGE_NODE_KINDS | NODE_KINDS


# ---------------------------------------------------------------------------
# Stale class enum (kg_services.stale_class column, добавлен PR #86)
# ---------------------------------------------------------------------------

#: Допустимые значения колонки `kg_services.stale_class`. Источник
#: реализации классификатора — `app.knowledge_graph.stale_classifier`.
#: Контракт фиксирует именно строковые значения (миграция хранит как
#: `String`, не PG enum, ради sqlite-compat тестов).
STALE_CLASS_ACTIVE: str = "active"
STALE_CLASS_EXPECTED_STALE: str = "expected_stale"
STALE_CLASS_SUSPICIOUS_STALE: str = "suspicious_stale"

STALE_CLASS_VALUES: Set[str] = {
    STALE_CLASS_ACTIVE,
    STALE_CLASS_EXPECTED_STALE,
    STALE_CLASS_SUSPICIOUS_STALE,
}


def is_edge_kind_known(kind: str) -> bool:
    """Известен ли edge kind контракту (в т.ч. planned)."""
    return kind in EDGE_KINDS


def active_edge_kinds() -> Set[str]:
    """Edge kinds со статусом 'active' (уже мерджены в master)."""
    return {k for k, spec in EDGE_KINDS.items() if spec["status"] == "active"}


def planned_edge_kinds() -> Set[str]:
    """Edge kinds со статусом 'planned' (ожидают merge соседних PR)."""
    return {k for k, spec in EDGE_KINDS.items() if spec["status"] == "planned"}


# ---------------------------------------------------------------------------
# Startup contract check
# ---------------------------------------------------------------------------

def STARTUP_CONTRACT_CHECK(db: "Session") -> Dict[str, object]:
    """Запускается при boot копилота. Сверяет реальное состояние БД с
    объявленным контрактом и логирует расхождения.

    Не throws на расхождения (graceful) — это диагностический
    инструмент, не gate. Возвращает report dict для тестов и
    self-health endpoint.

    Проверяем:
      * unknown_edge_kinds — kinds в БД которых нет в `EDGE_KINDS`.
      * planned_in_db — kinds со статусом 'planned' уже встречаются
        (значит wave замерджен, надо переключить status в 'active' и
        бампнуть SCHEMA_VERSION).
      * orphan_pct / owner_pct — текущие значения для дашборда.
    """
    from sqlalchemy import text

    report: Dict[str, object] = {
        "schema_version": KG_SCHEMA_VERSION,
        "unknown_edge_kinds": [],
        "planned_in_db": [],
        "orphan_pct": None,
        "owner_pct": None,
    }

    try:
        rows = db.execute(text(
            "SELECT DISTINCT kind FROM kg_service_edges"
        )).fetchall()
        db_kinds = {r[0] for r in rows if r and r[0]}
        unknown = sorted(db_kinds - set(EDGE_KINDS.keys()))
        already_active = sorted(db_kinds & planned_edge_kinds())
        report["unknown_edge_kinds"] = unknown
        report["planned_in_db"] = already_active
        if unknown:
            log.warning(
                "kg_contract.unknown_edge_kinds_in_db",
                extra={"kinds": unknown, "schema_version": KG_SCHEMA_VERSION},
            )
        if already_active:
            log.warning(
                "kg_contract.planned_kinds_already_in_db",
                extra={"kinds": already_active, "schema_version": KG_SCHEMA_VERSION},
            )
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning("kg_contract.check_failed", extra={"error": str(exc)})
        return report

    try:
        # Только service-узлы: owner-coverage — метрика про логические
        # сервисы, workload-узлы наследуют owner от своего Service и лишь
        # разбавили бы и числитель, и знаменатель.
        kind_params = {"svc_kind": NODE_KIND_SERVICE}
        services_total = db.execute(
            text("SELECT count(*) FROM kg_services WHERE node_kind = :svc_kind"),
            kind_params,
        ).scalar() or 0
        with_owner = db.execute(text(
            "SELECT count(*) FROM kg_services "
            "WHERE node_kind = :svc_kind "
            "  AND team_owner IS NOT NULL AND team_owner <> ''"
        ), kind_params).scalar() or 0
        # orphan_pct — единый источник (app-scope, excl expected_stale).
        report["orphan_pct"] = compute_orphan_stats(db)["orphan_pct"]
        if services_total > 0:
            report["owner_pct"] = round(100.0 * with_owner / services_total, 1)
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning("kg_contract.quality_check_failed", extra={"error": str(exc)})

    # Diagnostic-only: не падаем если ниже threshold'а. Дашборд берёт
    # эти значения и красит в красный/жёлтый отдельно.
    orphan_pct = report.get("orphan_pct")
    if isinstance(orphan_pct, (int, float)) and orphan_pct > QUALITY_THRESHOLDS["orphan_rate_max_pct"]:
        log.warning(
            "kg_contract.orphan_rate_above_threshold",
            extra={
                "actual_pct": orphan_pct,
                "threshold_pct": QUALITY_THRESHOLDS["orphan_rate_max_pct"],
            },
        )

    owner_pct = report.get("owner_pct")
    if isinstance(owner_pct, (int, float)) and owner_pct < QUALITY_THRESHOLDS["owner_coverage_min_pct"]:
        log.warning(
            "kg_contract.owner_coverage_below_threshold",
            extra={
                "actual_pct": owner_pct,
                "threshold_pct": QUALITY_THRESHOLDS["owner_coverage_min_pct"],
            },
        )

    # Physical-schema integrity. Регрессия 2026-06-01: restore из
    # `pg_dump --data-only` срезал PRIMARY KEY/индексы со ВСЕХ kg_* таблиц
    # (alembic_version при этом остался на head). Без PK суррогатный id
    # переставал быть уникальным → ORM `UPDATE WHERE id=` цеплял >1 строки
    # → StaleDataError → kg_alerts_resolve_sync/kg_jobs_sync падали каждый
    # прогон. Этот guard ловит такой дрейф на boot (log.error — не gate).
    try:
        rows = db.execute(text(
            "SELECT c.relname, EXISTS(SELECT 1 FROM pg_constraint x "
            "  WHERE x.conrelid = c.oid AND x.contype = 'p') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )).fetchall()
        missing_pk = sorted(r[0] for r in rows if r[0].startswith("kg_") and not r[1])
        report["missing_primary_key"] = missing_pk
        if missing_pk:
            log.error(
                "kg_contract.missing_primary_key",
                extra={"tables": missing_pk},
            )
    except Exception as exc:  # pragma: no cover - best-effort
        log.warning(
            "kg_contract.schema_integrity_check_failed", extra={"error": str(exc)}
        )

    return report


__all__ = [
    "KG_SCHEMA_VERSION",
    "REAL_SERVICE_KINDS",
    "SYNTHETIC_KINDS",
    "SERVICE_KINDS",
    "STORAGE_NODE_KINDS",
    "ALL_NODE_KINDS",
    "OWNER_SOURCES",
    "OWNER_SOURCE_ALIASES",
    "OWNER_SOURCE_TRUST",
    "OWNER_SOURCE_NAMESPACE_PREFIX",
    "OWNER_SOURCE_PLATFORM_STATIC",
    "OWNER_SOURCE_K8S_LABELS",
    "OWNER_SOURCE_MANUAL",
    "OWNER_SOURCE_DEPLOY_HISTORY",
    "OWNER_SOURCE_SUGGESTED",
    "owner_source_valid",
    "EDGE_KINDS",
    "EdgeKindSpec",
    "QUALITY_THRESHOLDS",
    "DEPLOY_ATTRIBUTION_WINDOW_DAYS",
    "EDGE_STALE_WINDOW_DAYS",
    "STALE_CLASS_ACTIVE",
    "STALE_CLASS_EXPECTED_STALE",
    "STALE_CLASS_SUSPICIOUS_STALE",
    "STALE_CLASS_VALUES",
    "is_synthetic",
    "is_orphan",
    "OrphanStats",
    "compute_orphan_stats",
    "owner_known",
    "service_kind_of",
    "is_edge_kind_known",
    "active_edge_kinds",
    "planned_edge_kinds",
    "STARTUP_CONTRACT_CHECK",
]
