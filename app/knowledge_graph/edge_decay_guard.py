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
from datetime import datetime, timedelta, timezone
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
# Срез PV. Рёбер сам по себе не даёт (`bound_to` строится от PVC), но это
# отдельный `kubectl get pv` со своим режимом отказа — и до 18.09.2026 он
# не отчитывался вовсе: PV-часть синка была невидима для покрытия
# источников, хотя именно её снимок решает, чистить ли узлы.
SOURCE_STORAGE_PVS = "k8s_storage_sync/pvs"

#: Все источники, отчитывающиеся через `record_source_run`. Нужен для
#: вопроса «а все ли вообще отработали»: до сих пор отчёты читал только сам
#: decay, и молчание источника никого не тревожило — оно не создаёт ошибок,
#: оно создаёт пустоту, неотличимую от «в кластере ничего нет».
#: Заводишь новый источник — добавляешь строку СЮДА, иначе он не попадёт в
#: покрытие и промолчит незаметно.
ALL_EDGE_SOURCES: tuple = (
    SOURCE_KG_SYNC,
    SOURCE_TOPOLOGY_SERVICES,
    SOURCE_TOPOLOGY_INGRESSES,
    SOURCE_INGRESS_SYNC,
    SOURCE_NATS_SUBJECTS_SYNC,
    SOURCE_STORAGE_PODS,
    SOURCE_STORAGE_PVCS,
    SOURCE_STORAGE_PVS,
)


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
    SOURCE_STORAGE_PVS: "pvs_fetched",
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
    #: Итог чистки узлов у тех источников, которые её делают: причина
    #: пропуска и цифры, по которым видно, доверять ли снимку. В отличие от
    #: `raw` переносится в redis — потому что читать это должен не сам
    #: прогон, а self-health в соседнем процессе.
    cleanup: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


# In-process кэш отчётов. С 17.09.2026 он больше не единственный слой:
# `record_source_run` дублирует отчёт в redis, и `get_source_report` читает
# оттуда, если в этом процессе отчёта нет. Раньше отчёт чужого синка
# соседний форк не видел вовсе — celery раскидывает beat-таски по
# процессам, — и `check_source_coverage` не мог отличить «источник молчит»
# от «этот форк не видел прогона», а decay терял точную причину и
# откатывался на общий `no_recent_refresh`.
#
# Память остаётся первой ступенью: она быстрее и переживает недоступный
# redis. Отдельной таблицы для этого не нужно — истории здесь не хранится,
# только последний отчёт на источник.
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

    cleanup_raw = payload.get("cleanup")
    cleanup: Dict[str, Any] = {}
    if isinstance(cleanup_raw, dict):
        # Только то, по чему принимается решение, — не весь блок: значение
        # едет в redis и не должно расти вместе со stats синка.
        for key in ("skipped", "shrink_pct", "baseline",
                    "bootstrap_baseline", "rows_total"):
            if cleanup_raw.get(key) not in (None, ""):
                cleanup[key] = cleanup_raw[key]

    report = SourceReport(
        source=source,
        ts=now or datetime.utcnow(),
        fetched=fetched,
        errors=errors,
        failed=bool(payload.get("error")),
        cleanup=cleanup,
        raw=payload,
    )
    _REPORTS[source] = report
    _persist_report(report)
    return report


def _persist_report(report: SourceReport) -> None:
    """Продублировать отчёт в redis — чтобы его увидел соседний процесс.

    Fail-open и намеренно молча: синк свою работу уже сделал, и падать на
    недоступном redis из-за телеметрии он не должен. Ошибку залогирует сам
    слой записи.
    """
    try:
        from app.services.digest.state import record_source_report

        record_source_report(report.source, {
            "ts": report.ts.isoformat(),
            "fetched": report.fetched,
            "errors": report.errors,
            "failed": report.failed,
            "cleanup": report.cleanup,
        })
    except Exception:  # noqa: BLE001 — телеметрия не роняет синк
        pass


def _report_from_redis(source: str) -> Optional[SourceReport]:
    """Отчёт соседнего процесса. None — ключа нет, redis лёг или мусор.

    `raw` намеренно не переносится: он нужен только для отладки внутри
    одного прогона, а в redis раздувал бы значение полным stats-словарём
    синка. Решения принимаются по fetched/errors/failed и по компактному
    `cleanup` — последний переносится, потому что его читателем как раз и
    является соседний процесс (self-health).
    """
    try:
        from app.services.digest.state import get_source_report

        data = get_source_report(source)
        if not data:
            return None
        ts_raw = data.get("ts")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else None
        if ts is None:
            return None
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        fetched = data.get("fetched")
        cleanup = data.get("cleanup")
        return SourceReport(
            source=source,
            ts=ts,
            fetched=int(fetched) if fetched is not None else None,
            errors=int(data.get("errors") or 0),
            failed=bool(data.get("failed")),
            cleanup=cleanup if isinstance(cleanup, dict) else {},
            raw={},
        )
    except Exception:  # noqa: BLE001 — читаем best-effort
        return None


def reset_source_reports() -> None:
    """Очистить реестр отчётов. Для тестов — реестр живёт на уровне модуля."""
    _REPORTS.clear()


def get_source_report(source: str) -> Optional[SourceReport]:
    """Самый СВЕЖИЙ отчёт источника: из своего процесса или из redis.

    Берётся тот, у кого позже `ts`, а не локальный по умолчанию. Разница
    существенная: следующий прогон того же синка мог уйти в другой форк и
    записать в redis свежий отчёт — упавший или с пустым fetch'ем. Отдай
    мы локальный просто потому, что он свой, старый здоровый отчёт
    маскировал бы новый сбойный на всё окно свежести, и decay считал бы
    источник живым.

    Redis-ступень нужна и сама по себе: синк отработал в ОДНОМ форке, а
    решение принимается в другом — до 17.09.2026 такой отчёт для читателя
    просто не существовал.

    Fail-open: redis недоступен → остаётся локальный, как раньше.
    """
    local = _REPORTS.get(source)
    remote = _report_from_redis(source)
    if local is None:
        return remote
    if remote is None:
        return local
    return remote if remote.ts > local.ts else local


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
        # Читаем через get_source_report: с 17.09.2026 он берёт отчёт и из
        # redis, поэтому прогон из соседнего форка больше не невидим —
        # раньше в таком случае терялась точная причина (empty_fetch,
        # fetch_errors) и всё сводилось к общему no_recent_refresh.
        report = get_source_report(source)
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
