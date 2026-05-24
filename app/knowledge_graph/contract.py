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
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Set, TypedDict

if TYPE_CHECKING:  # pragma: no cover - only for typing
    from sqlalchemy.orm import Session

    from app.knowledge_graph.schema import Service


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

#: KG schema contract version. Bump rules — см. docs/KG_SCHEMA_CONTRACT.md.
#: 2.1 = после Wave 7 (PodEvent corr / Service+Ingress topology / NATS subjects).
KG_SCHEMA_VERSION: str = "2.1"


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


# ---------------------------------------------------------------------------
# Owner sources
# ---------------------------------------------------------------------------

#: Откуда мог быть взят `kg_services.team_owner`. Сейчас в схеме поле
#: одно (без `owner_source`), но мы фиксируем семантику чтобы дальнейшие
#: волны могли начать прокидывать `owner_source` как metadata-key.
OWNER_SOURCES: Set[str] = {
    "manual",            # ручная правка через admin endpoint (планируется PR #19)
    "k8s_labels",        # лейбл `team-owner` / `owner` на Deployment/StS
    "namespace_prefix",  # эвристика по namespace (`squad-N` → owner=`squad-N`)
    "platform_static",   # synthetic-узлы platform/data/external — захардкожено
    "suggested",         # AI/heuristic suggestion (планируется PR #18, нужно approve)
}


# ---------------------------------------------------------------------------
# Edge kinds inventory
# ---------------------------------------------------------------------------

class EdgeKindSpec(TypedDict):
    """Описание одного edge kind."""
    semantic: str          # что edge значит человеческими словами
    src_kinds: Set[str]    # допустимые kinds у src service
    dst_kinds: Set[str]    # допустимые kinds у dst service
    source: str            # какой sync/parser создаёт edge
    example: str           # пример (для документации)
    status: str            # "active" | "planned"


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
    },
    "uses_db": {
        "semantic": "src читает/пишет в БД (synthetic db-узел)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": {"db"},
        "source": "kg_sync (env_vars: *_CONN / *_DSN)",
        "example": "town-service --uses_db--> db:postgres:postgres-squad-1",
        "status": "active",
    },
    "uses_nats": {
        "semantic": "src подключается к NATS-кластеру",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": {"nats", "subject"},
        "source": "kg_sync (nats_env) + nats_subjects_sync (Wave 7-Z)",
        "example": "town-service --uses_nats--> subject:march-export",
        "status": "active",
    },
    "serves_traffic": {
        "semantic": "src получает HTTP-трафик через ingress dst",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": {"ingress"},
        "source": "k8s_topology_resources_sync (Wave 7 / G1.3)",
        "example": "wo-api-squad-1 --serves_traffic--> ingress:wo-api-squad-1.lastoasisgame.com",
        "status": "active",
    },
    "routes_to": {
        "semantic": "ingress правило роутит на backend service",
        "src_kinds": {"ingress"},
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "k8s_topology_resources_sync (Wave 7 / G1.3)",
        "example": "ingress:wo-api-squad-1.* --routes_to--> wo-api-squad-1",
        "status": "active",
    },
    "pod_event_of": {
        "semantic": "Pod event (OOMKilled/CrashLoop) принадлежит сервису",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "runtime_correlation (Wave 7-Y)",
        "example": "kg_pod_events row linked → kg_services row (через service_id FK)",
        "status": "active",
    },
    # ---- Planned ----
    "runs_as_job": {
        "semantic": "Service запускается как k8s Job/CronJob (не Deployment)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "k8s_jobs_sync (planned in PR #16)",
        "example": "backup-cron-town --runs_as_job--> (self, schedule='0 */6 * * *')",
        "status": "planned",
    },
    "uses_volume": {
        "semantic": "Service монтирует PV/PVC (storage dependency)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,  # synthetic volume kind может появиться позже
        "source": "k8s_storage_sync (planned in PR #17)",
        "example": "postgres-squad-1 --uses_volume--> pvc:data-postgres-squad-1-0",
        "status": "planned",
    },
    "bound_to": {
        "semantic": "PVC связан с конкретным PV (для cluster-PV резерва)",
        "src_kinds": REAL_SERVICE_KINDS,
        "dst_kinds": REAL_SERVICE_KINDS,
        "source": "k8s_storage_sync (planned in PR #17)",
        "example": "pvc:data-postgres-squad-1-0 --bound_to--> pv:pvc-abc123",
        "status": "planned",
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
) -> bool:
    """Orphan service по контракту:

      * НЕ synthetic, И
      * нет ни одной edge'и (src или dst), И
      * нет deploy'я за последние `DEPLOY_ATTRIBUTION_WINDOW_DAYS`.

    `edge_ids_seen` — итерируемое со всеми service_id, которые
    встречаются как src или dst в kg_service_edges (caller получает
    через `SELECT DISTINCT src_id UNION DISTINCT dst_id`).

    Параметр `has_recent_deploy` опциональный: caller может пропустить
    deploy-проверку, передав default=False; тогда orphan определяется
    только по edges (это поведение текущей stats_digest.kg_quality_section).
    """
    if is_synthetic(service):
        return False
    svc_id = getattr(service, "id", None)
    if svc_id is None:
        return False
    if svc_id in set(edge_ids_seen):
        return False
    if has_recent_deploy:
        return False
    return True


def owner_known(service: "Service") -> bool:
    """Заполнен ли `team_owner` (не None, не пустая строка, не 'unknown')."""
    owner: Optional[str] = getattr(service, "team_owner", None)
    if not owner:
        return False
    if owner.strip().lower() in {"unknown", "n/a", "-", "none"}:
        return False
    return True


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
        services_total = db.execute(
            text("SELECT count(*) FROM kg_services")
        ).scalar() or 0
        synthetic_count = db.execute(
            text("SELECT count(*) FROM kg_services WHERE synthetic = true")
        ).scalar() or 0
        with_owner = db.execute(text(
            "SELECT count(*) FROM kg_services "
            "WHERE team_owner IS NOT NULL AND team_owner <> ''"
        )).scalar() or 0
        orphan = db.execute(text("""
            SELECT count(*) FROM kg_services s
            WHERE NOT s.synthetic
              AND s.id NOT IN (
                  SELECT src_id FROM kg_service_edges
                  UNION SELECT dst_id FROM kg_service_edges
              )
        """)).scalar() or 0
        real_total = services_total - synthetic_count
        if real_total > 0:
            report["orphan_pct"] = round(100.0 * orphan / real_total, 1)
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

    return report


__all__ = [
    "KG_SCHEMA_VERSION",
    "REAL_SERVICE_KINDS",
    "SYNTHETIC_KINDS",
    "SERVICE_KINDS",
    "OWNER_SOURCES",
    "EDGE_KINDS",
    "EdgeKindSpec",
    "QUALITY_THRESHOLDS",
    "DEPLOY_ATTRIBUTION_WINDOW_DAYS",
    "EDGE_STALE_WINDOW_DAYS",
    "is_synthetic",
    "is_orphan",
    "owner_known",
    "service_kind_of",
    "is_edge_kind_known",
    "active_edge_kinds",
    "planned_edge_kinds",
    "STARTUP_CONTRACT_CHECK",
]
