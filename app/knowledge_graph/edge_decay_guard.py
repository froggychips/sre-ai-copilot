"""Kind/source-aware guard для edge-decay — защита KG от «тихой эрозии».

ПРОБЛЕМА
--------
`kg_sync._decay_stale_edges` гасит (`inactive`) и удаляет рёбра
`kg_service_edges` по возрасту `last_seen_at`. Но `last_seen_at` разных
`kind` освежают РАЗНЫЕ модули, и каждый из них живёт в своём beat-таске:

    serves_traffic  ← k8s_topology_resources_sync, срез services
    routes_to       ← k8s_topology_resources_sync, срез ingresses
    calls           ← kg_sync (env-scan) + k8s_ingress_sync (ingress-срез)
    uses_db         ← kg_sync (env-scan)
    uses_nats       ← kg_sync (env-scan) + nats_subjects_sync (парсер монорепы)

Все они НАМЕРЕННО глотают свои сбои (`kubectl` упал → `return []`, а не
raise), чтобы failure одного тика не валила beat-loop. А deadman у decay
получал `has_fetch_errors` ТОЛЬКО из собственных счётчиков `kg_sync`.

Отсюда задокументированный инцидент: `kubectl get services -A` (42 МБ JSON)
стабильно таймаутил, `services_fetched=0` каждый тик → рёбра
`serves_traffic` никто не освежал → через `inactive_after_days` они гасли,
через `delete_after_days` удалялись. Порог `EDGE_DECAY_MAX_DELETE_PCT` (25%)
не спасал: рёбра стареют постепенно и вырезаются порциями меньше порога.
Целые классы топологии эродировали без единого сигнала.

РЕШЕНИЕ
-------
Класс рёбер можно децаить ТОЛЬКО если синхронизатор, отвечающий за свежесть
этого kind, реально отработал в этом цикле. Здоровье источника собирается из
ДВУХ сигналов, в порядке приоритета:

  1. Per-cycle stats-отчёт. Синк в конце своего прогона зовёт
     `record_source_run(SOURCE, stats)` и отдаёт СВОЙ уже существующий
     stats-словарь (`errors`, `*_fetched`). Отчёт видит: упал ли синк
     (`error`), были ли fetch-ошибки (`errors > 0`), не вернул ли он
     подозрительный ноль (`*_fetched == 0` при непустом прошлом состоянии).
     Это самый точный и своевременный сигнал.

  2. Фоллбэк по данным: `max(last_seen_at)` по рёбрам источника. Работает,
     когда свежего отчёта нет — синк живёт в ДРУГОМ процессе (beat-таски
     раскиданы celery-worker'ом по forked-процессам), поэтому in-process
     реестр отчётов межпроцессно не виден. Тогда судим по факту: освежил ли
     источник хоть одно своё ребро за окно `KG_EDGE_SOURCE_FRESH_HOURS`.

Fail-closed: kind, не сопоставленный НИ ОДНОМУ источнику, не децаится
никогда — иначе новый kind начнёт молча эродировать ровно так же, как
`serves_traffic`. Пропуск всегда логируется warning'ом: молчаливый пропуск
недопустим, исходная беда была именно в отсутствии сигнала.

Дисциплина «пустой fetch неотличим от пустого кластера → не чистим»
зеркалит `k8s_jobs_sync.cleanup_stale_jobs` и `drift_cleanup`.

ТАБЛИЦЫ
-------
Тот же механизм обслуживает ДВЕ таблицы рёбер: `kg_service_edges` (карта
`EDGE_KIND_FRESHNESS_SOURCES`) и `kg_volume_edges` (карта
`VOLUME_EDGE_KIND_FRESHNESS_SOURCES`, decay живёт в
`k8s_storage_sync.decay_volume_edges`). Карты раздельные намеренно: kind'ы
не пересекаются, а смешивать инвентарь двух таблиц в одном источнике —
значит чинить одно, а ломать другое.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import ServiceEdge, VolumeEdge

logger = logging.getLogger(__name__)


# ── Имена источников свежести ───────────────────────────────────────────────
#
# Гранулярность = единица fetch'а, а не beat-таск. `k8s_topology_resources_sync`
# делает ДВА независимых `kubectl get` (services и ingresses), и в реальном
# инциденте таймаутил ровно первый. Если бы источник был один на оба среза,
# сбой services замораживал бы ещё и decay `routes_to` — безопасно, но
# бессмысленно широко.
SOURCE_KG_SYNC = "kg_sync"
SOURCE_TOPOLOGY_SERVICES = "k8s_topology_resources_sync/services"
SOURCE_TOPOLOGY_INGRESSES = "k8s_topology_resources_sync/ingresses"
SOURCE_INGRESS_SYNC = "k8s_ingress_sync"
SOURCE_NATS_SUBJECTS_SYNC = "nats_subjects_sync"
# Storage-слой (`kg_volume_edges`). Тоже по единице fetch'а: `uses_volume`
# живёт от cluster-wide среза pod'ов, `bound_to` — от среза PVC. Срезы
# независимы (разные `kubectl get`, разные объёмы, разные режимы отказа).
SOURCE_STORAGE_PODS = "k8s_storage_sync/pods"
SOURCE_STORAGE_PVCS = "k8s_storage_sync/pvcs"


# ── ЕДИНОЕ МЕСТО: kind ребра → синхронизатор, освежающий его last_seen_at ───
#
# ЭТО ТА САМАЯ КАРТА. Заводишь новый kind в `kg_service_edges` — добавляешь
# строку СЮДА, иначе рёбра нового kind навсегда исключаются из decay
# (fail-closed) и в логах повиснет `unmapped_kind`.
#
# Значение — кортеж, потому что один kind может освежаться НЕСКОЛЬКИМИ
# синками (`calls` пишут и env-scan `kg_sync`, и `k8s_ingress_sync`).
# Точная атрибуция конкретного ребра к одному из них делается по
# `discovered_by` (см. EDGE_DISCOVERED_BY_SOURCE ниже).
#
# Источник истины по семантике kind'ов — `contract.EDGE_KINDS` (там же
# видно, какие kind'ы вообще живут в `kg_service_edges`, а какие —
# `fk_only`/`metadata_only` и сюда не относятся).
EDGE_KIND_FRESHNESS_SOURCES: Dict[str, Tuple[str, ...]] = {
    "calls": (SOURCE_KG_SYNC, SOURCE_INGRESS_SYNC),
    "uses_db": (SOURCE_KG_SYNC,),
    "uses_nats": (SOURCE_KG_SYNC, SOURCE_NATS_SUBJECTS_SYNC),
    "serves_traffic": (SOURCE_TOPOLOGY_SERVICES,),
    "routes_to": (SOURCE_TOPOLOGY_INGRESSES,),
}

# Точная атрибуция ребра: `discovered_by` → источник. Нужна там, где kind
# сам по себе неоднозначен (`calls`, `uses_nats`).
EDGE_DISCOVERED_BY_SOURCE: Dict[str, str] = {
    "kg_sync/env_vars": SOURCE_KG_SYNC,
    "kg_sync/env_url_v2": SOURCE_KG_SYNC,
    "kg_sync/nats_env": SOURCE_KG_SYNC,
    "kg_sync/dsn_env": SOURCE_KG_SYNC,
    "kg_sync/secret_hint": SOURCE_KG_SYNC,
    "kg_sync/runtime_seen": SOURCE_KG_SYNC,
    # Ниже — чужие синки, несмотря на исторический префикс `kg_sync/`.
    "kg_sync/ingress": SOURCE_INGRESS_SYNC,
    "kg_sync/nats_subjects_parser": SOURCE_NATS_SUBJECTS_SYNC,
    "k8s_topology_resources/service": SOURCE_TOPOLOGY_SERVICES,
    "k8s_topology_resources/ingress": SOURCE_TOPOLOGY_INGRESSES,
}

# ── То же для `kg_volume_edges` (storage-слой, k8s_storage_sync) ────────────
#
# Отдельная таблица → отдельная карта. Оба kind'а однозначны, поэтому
# legacy-группа (см. LEGACY_SOURCE_PREFIX) тут не нужна: даже ребро без
# `discovered_by` атрибутируется по kind'у.
VOLUME_EDGE_KIND_FRESHNESS_SOURCES: Dict[str, Tuple[str, ...]] = {
    "uses_volume": (SOURCE_STORAGE_PODS,),
    "bound_to": (SOURCE_STORAGE_PVCS,),
}

VOLUME_EDGE_DISCOVERED_BY_SOURCE: Dict[str, str] = {
    "k8s_storage/pod_volumes": SOURCE_STORAGE_PODS,
    "k8s_storage/pvc_spec": SOURCE_STORAGE_PVCS,
}

# Префикс псевдо-источника для legacy-рёбер: kind известен и неоднозначен, а
# `discovered_by` пуст/незнаком — атрибутировать такое ребро к конкретному
# синку нельзя. Судим группу по ней самой: если в ней есть свежие рёбра,
# кто-то её пишет, decay допустим.
LEGACY_SOURCE_PREFIX = "kind:"

# Ключ stats-словаря, по которому у источника определяется «сколько объектов
# реально получено из внешней системы». Ноль по этому ключу при непустом
# прошлом состоянии = подозрительный ноль (сбой fetch неотличим от пустого
# кластера). Ключи — те, что синки УЖЕ отдают, новых счётчиков не заводим.
_SOURCE_FETCH_KEY: Dict[str, str] = {
    # Для kg_sync единица наблюдения — успешно просканированный namespace,
    # а не найденные сервисы: пустой список деплойментов в ns — это норма.
    SOURCE_KG_SYNC: "namespaces",
    SOURCE_TOPOLOGY_SERVICES: "services_fetched",
    SOURCE_TOPOLOGY_INGRESSES: "ingresses_fetched",
    SOURCE_INGRESS_SYNC: "ingresses_fetched",
    SOURCE_NATS_SUBJECTS_SYNC: "files_scanned",
    # Storage: единица наблюдения `uses_volume` — просканированный pod
    # (cluster-wide лист pod'ов — самый тяжёлый fetch во всём KG), `bound_to`
    # — полученный PVC.
    SOURCE_STORAGE_PODS: "pods_scanned",
    SOURCE_STORAGE_PVCS: "pvcs_fetched",
}

# Окно свежести по умолчанию. Должно быть больше максимального интервала
# beat-тасков (самый редкий — nats_subjects, 6ч), с запасом на рестарты.
_EDGE_SOURCE_FRESH_HOURS_DEFAULT = 24

# ── Причины блокировки decay (попадают в логи и в stats) ────────────────────
REASON_UNMAPPED_KIND = "unmapped_kind"
REASON_SYNC_FAILED = "sync_failed"
REASON_FETCH_ERRORS = "fetch_errors"
REASON_EMPTY_FETCH = "empty_fetch"
REASON_NO_RECENT_REFRESH = "no_recent_refresh"


@dataclass(frozen=True)
class SourceReport:
    """Отчёт синка о своём прогоне за цикл."""

    source: str
    ts: datetime
    #: Сколько объектов получено из внешней системы. None — у источника нет
    #: fetch-счётчика в stats, судить по нулю нельзя.
    fetched: Optional[int]
    errors: int
    #: Синк завершился аварийно целиком (вернул `{"error": ...}`).
    failed: bool
    raw: Dict[str, Any] = field(default_factory=dict)


# In-process реестр отчётов. ОГРАНИЧЕНИЕ: celery-worker раскидывает beat-таски
# по forked-процессам, поэтому отчёт чужого синка kg_sync увидит только если
# они попали в один процесс. Это не дыра, а слой: когда отчёта нет, решение
# принимает фоллбэк по данным (`max(last_seen_at)`), который межпроцессный по
# определению. Апгрейд до durable-хранилища потребует таблицы (миграции).
_REPORTS: Dict[str, SourceReport] = {}


def record_source_run(
    source: str,
    stats: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> SourceReport:
    """Принять stats-отчёт синка за цикл. Зовётся САМИМ синком в конце run-а.

    `stats` — уже существующий stats-словарь синка, ничего специально
    считать не надо. Распознаются ключи:
      * `error`     — синк упал целиком (nats_subjects_sync так рапортует);
      * `errors`    — счётчик per-item сбоев;
      * fetch-ключ источника из `_SOURCE_FETCH_KEY`.
    """
    payload: Dict[str, Any] = dict(stats or {})
    fetched: Optional[int] = None
    key = _SOURCE_FETCH_KEY.get(source)
    if key is not None and key in payload:
        try:
            fetched = int(payload[key] or 0)
        except (TypeError, ValueError):
            fetched = None
    try:
        errors = int(payload.get("errors") or 0)
    except (TypeError, ValueError):
        errors = 0

    report = SourceReport(
        source=source,
        ts=now or datetime.utcnow(),
        fetched=fetched,
        errors=errors,
        failed=bool(payload.get("error")),
        raw=payload,
    )
    _REPORTS[source] = report
    return report


def reset_source_reports() -> None:
    """Очистить реестр отчётов. Для тестов — реестр живёт на уровне модуля."""
    _REPORTS.clear()


def get_source_report(source: str) -> Optional[SourceReport]:
    """Последний отчёт источника (или None)."""
    return _REPORTS.get(source)


def _resolve_sources(
    kind: Optional[str],
    discovered_by: Optional[str],
    kind_map: Mapping[str, Tuple[str, ...]],
    dby_map: Mapping[str, str],
    dynamic_prefixes: Mapping[str, str],
) -> Tuple[str, ...]:
    """Общее ядро атрибуции ребра к источнику (обе таблицы рёбер).

    `dynamic_prefixes` — синки, которые собирают `discovered_by` на лету
    (`kg_sync/{source}`): всё, что не перехвачено явной картой, но начинается
    с префикса, отдаём владельцу префикса.
    """
    dby = (discovered_by or "").strip()
    exact = dby_map.get(dby)
    if exact:
        return (exact,)

    kind_key = (kind or "").strip()
    sources = kind_map.get(kind_key)
    if not sources:
        return ()

    for prefix, owner in dynamic_prefixes.items():
        if dby.startswith(prefix):
            return (owner,)
    if len(sources) == 1:
        # kind однозначен — атрибутируем даже без discovered_by.
        return sources
    # kind неоднозначен и автор неизвестен → legacy-группа, судит сама себя.
    return (f"{LEGACY_SOURCE_PREFIX}{kind_key}",)


# kg_sync собирает `discovered_by` динамически (`kg_sync/{source}` для
# uses_db) — перечислить все значения в карте нельзя.
_DYNAMIC_PREFIX_SOURCES: Dict[str, str] = {"kg_sync/": SOURCE_KG_SYNC}


def resolve_edge_sources(
    kind: Optional[str],
    discovered_by: Optional[str],
) -> Tuple[str, ...]:
    """Источник свежести ребра `kg_service_edges`.

    Возвращает ПУСТОЙ кортеж, если kind не сопоставлен ни одному источнику —
    это fail-closed сигнал «децаить нельзя, никто не отвечает за свежесть».
    """
    return _resolve_sources(
        kind, discovered_by,
        EDGE_KIND_FRESHNESS_SOURCES, EDGE_DISCOVERED_BY_SOURCE,
        _DYNAMIC_PREFIX_SOURCES,
    )


def resolve_volume_edge_sources(
    kind: Optional[str],
    discovered_by: Optional[str],
) -> Tuple[str, ...]:
    """Источник свежести ребра `kg_volume_edges`. Fail-closed так же."""
    return _resolve_sources(
        kind, discovered_by,
        VOLUME_EDGE_KIND_FRESHNESS_SOURCES, VOLUME_EDGE_DISCOVERED_BY_SOURCE,
        {},
    )


def _fresh_hours() -> int:
    from app.config import settings

    try:
        return int(getattr(
            settings, "KG_EDGE_SOURCE_FRESH_HOURS",
            _EDGE_SOURCE_FRESH_HOURS_DEFAULT,
        ))
    except (TypeError, ValueError):
        return _EDGE_SOURCE_FRESH_HOURS_DEFAULT


def _inventory(db: Session, model: Any, resolver: Any) -> Dict[str, Dict[str, Any]]:
    """Инвентарь таблицы рёбер по источникам: {source: {edges, last_seen}}.

    Один GROUP BY по (kind, discovered_by) — дешевле, чем тянуть рёбра.
    `model` — ServiceEdge либо VolumeEdge (обе имеют kind/discovered_by/
    last_seen_at), `resolver` — соответствующая функция атрибуции.
    """
    rows = (
        db.query(
            model.kind,
            model.discovered_by,
            func.count(model.id),
            func.max(model.last_seen_at),
        )
        .group_by(model.kind, model.discovered_by)
        .all()
    )
    inv: Dict[str, Dict[str, Any]] = {}
    for kind, dby, cnt, max_seen in rows:
        for source in resolver(kind, dby):
            slot = inv.setdefault(source, {"edges": 0, "last_seen": None})
            slot["edges"] += int(cnt or 0)
            prev = slot["last_seen"]
            if max_seen is not None and (prev is None or max_seen > prev):
                slot["last_seen"] = max_seen
    return inv


def source_inventory(db: Session) -> Dict[str, Dict[str, Any]]:
    """Инвентарь `kg_service_edges` по источникам."""
    return _inventory(db, ServiceEdge, resolve_edge_sources)


def volume_source_inventory(db: Session) -> Dict[str, Dict[str, Any]]:
    """Инвентарь `kg_volume_edges` по источникам."""
    return _inventory(db, VolumeEdge, resolve_volume_edge_sources)


def _grade_report(report: SourceReport) -> Optional[str]:
    """Причина, по которой отчёт считается нездоровым (или None)."""
    if report.failed:
        return REASON_SYNC_FAILED
    if report.errors > 0:
        return REASON_FETCH_ERRORS
    if report.fetched is not None and report.fetched <= 0:
        # Ноль объектов при непустом прошлом состоянии: сбой fetch
        # неотличим от реально опустевшего кластера, децаить нельзя.
        return REASON_EMPTY_FETCH
    return None


def unhealthy_sources(
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Источники `kg_service_edges`, которым нельзя доверить decay.

    Сигналы НЕЗАВИСИМЫ и складываются по «худшему»: источник здоров, только
    если чист и отчёт, и данные. Здоровый отчёт НЕ отменяет data-сигнал —
    иначе один успешный прогон легализовал бы удаление рёбер, которых этот
    прогон не касался (это ослабило бы уже существующий предохранитель).

    Рассматриваются только источники, у которых в графе ЕСТЬ рёбра — защищать
    нечего, если источник ничего не писал никогда.
    """
    return _unhealthy(source_inventory(db), now)


def unhealthy_volume_sources(
    db: Session,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """То же для `kg_volume_edges`: {source: reason}.

    Считается по инвентарю СВОЕЙ таблицы: сбой pod-среза не должен
    блокировать decay `bound_to`, и наоборот.
    """
    return _unhealthy(volume_source_inventory(db), now)


def _unhealthy(
    inventory: Mapping[str, Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Общее ядро: инвентарь источников → {source: reason} для нездоровых."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=_fresh_hours())

    bad: Dict[str, str] = {}
    for source, slot in inventory.items():
        if slot["edges"] <= 0:
            continue
        # Сигнал 1: свежий per-cycle отчёт синка — самый точный и
        # actionable, поэтому его причина имеет приоритет в логах.
        report = _REPORTS.get(source)
        if report is not None and report.ts >= cutoff:
            reason = _grade_report(report)
            if reason:
                bad[source] = reason
                continue
        # Сигнал 2: отчёта нет (синк в другом процессе — реестр in-process)
        # либо отчёт чист. В обоих случаях спрашиваем данные: освежил ли
        # источник хоть одно своё ребро за окно.
        last_seen = slot["last_seen"]
        if last_seen is None or last_seen < cutoff:
            bad[source] = REASON_NO_RECENT_REFRESH
    return bad


def _block_reason(
    sources: Tuple[str, ...],
    unhealthy: Mapping[str, str],
) -> Optional[str]:
    """Fail-closed: kind без сопоставленного источника блокируется всегда."""
    if not sources:
        return REASON_UNMAPPED_KIND
    for source in sources:
        reason = unhealthy.get(source)
        if reason:
            return reason
    return None


def edge_block_reason(
    kind: Optional[str],
    discovered_by: Optional[str],
    unhealthy: Mapping[str, str],
) -> Optional[str]:
    """Причина, по которой ребро `kg_service_edges` нельзя децаить (или None)."""
    return _block_reason(resolve_edge_sources(kind, discovered_by), unhealthy)


def volume_edge_block_reason(
    kind: Optional[str],
    discovered_by: Optional[str],
    unhealthy: Mapping[str, str],
) -> Optional[str]:
    """То же для ребра `kg_volume_edges`."""
    return _block_reason(
        resolve_volume_edge_sources(kind, discovered_by), unhealthy,
    )
