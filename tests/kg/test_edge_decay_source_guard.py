"""Kind/source-aware edge-decay: класс рёбер не эродирует, пока молчит его синк.

Инцидент, который закрывают эти тесты: `kubectl get services -A` (42 МБ JSON)
стабильно таймаутил, `k8s_topology_resources_sync` возвращал [] и НЕ raise-ил
(«failure в одном тике не должна валить beat-loop»), `services_fetched=0`
каждый тик. Рёбра `serves_traffic` никто не освежал → через
`inactive_after_days` гасли, через `delete_after_days` удалялись. Deadman у
decay получал `has_fetch_errors` ТОЛЬКО из счётчиков самого `kg_sync` и об
этом не знал, а 25%-cap не срабатывал: рёбра стареют постепенно и вырезаются
порциями меньше порога. Целый класс топологии исчезал молча.

Ключевой сценарий здесь — `test_stats_empty_fetch_blocks_decay_despite_fresh_edges`:
данных мало, чтобы поймать деградацию (свежие рёбра прошлого тика ещё лежат в
окне свежести), и только per-cycle stats-отчёт синка показывает, что fetch
вернул ноль.

Всё на in-memory SQLite — та же семантика, что у PG.
"""
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.edge_decay_guard import (
    EDGE_KIND_FRESHNESS_SOURCES, REASON_EMPTY_FETCH, REASON_FETCH_ERRORS,
    REASON_NO_RECENT_REFRESH, REASON_SYNC_FAILED, REASON_UNMAPPED_KIND,
    SOURCE_INGRESS_SYNC, SOURCE_KG_SYNC, SOURCE_NATS_SUBJECTS_SYNC,
    SOURCE_STORAGE_PODS, SOURCE_STORAGE_PVCS, SOURCE_TOPOLOGY_INGRESSES,
    SOURCE_TOPOLOGY_SERVICES, VOLUME_EDGE_KIND_FRESHNESS_SOURCES,
    edge_block_reason, record_source_run, resolve_edge_sources,
    resolve_volume_edge_sources, unhealthy_sources, unhealthy_volume_sources,
    volume_edge_block_reason,
)
from app.knowledge_graph.kg_sync import _decay_stale_edges
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import ServiceEdge, VolumeEdge

KG_SYNC_LOGGER = "app.knowledge_graph.kg_sync"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _edge(db, dst_name, kind, discovered_by=None, age_days=0):
    """Ребро src→dst_name заданного kind с искусственным возрастом."""
    src = upsert_service(db, "squad-1", "src")
    dst = upsert_service(db, "squad-1", dst_name)
    db.flush()
    e = upsert_edge(db, src, dst, kind=kind, discovered_by=discovered_by)
    e.last_seen_at = datetime.utcnow() - timedelta(days=age_days)
    db.flush()
    return e


def _healthy_calls_baseline(db):
    """Три свежих `calls`-ребра: источник kg_sync выглядит живым и по
    данным, и по объёму (нужен как знаменатель для 25%-cap)."""
    for i in range(3):
        _edge(db, f"c-fresh-{i}", "calls", "kg_sync/env_vars", age_days=0)


def _names(db):
    return {e.dst.name for e in db.query(ServiceEdge).all()}


def _inactive(db, name):
    edge = next(e for e in db.query(ServiceEdge).all() if e.dst.name == name)
    return bool((edge.extras or {}).get("inactive"))


# ── карта kind → источник ────────────────────────────────────────────────────


def test_every_live_edge_kind_is_mapped_to_a_source():
    """Карта покрывает все kind'ы, реально живущие в kg_service_edges.
    Забыли kind — он молча выпадет из decay (fail-closed), поэтому пусть
    падает тест, а не прод."""
    from app.knowledge_graph.contract import EDGE_KINDS

    live = {
        name for name, spec in EDGE_KINDS.items()
        if spec.get("table") == "kg_service_edges" and spec.get("status") == "active"
    }
    assert live, "контракт не отдал ни одного live edge-kind — проверь фильтр"
    assert live <= set(EDGE_KIND_FRESHNESS_SOURCES)


@pytest.mark.parametrize(
    "kind,discovered_by,expected",
    [
        # Однозначные kind'ы атрибутируются даже без discovered_by.
        ("serves_traffic", None, SOURCE_TOPOLOGY_SERVICES),
        ("routes_to", None, SOURCE_TOPOLOGY_INGRESSES),
        ("uses_db", None, SOURCE_KG_SYNC),
        # Неоднозначные — по discovered_by.
        ("calls", "kg_sync/env_vars", SOURCE_KG_SYNC),
        ("calls", "kg_sync/ingress", SOURCE_INGRESS_SYNC),
        ("uses_nats", "kg_sync/nats_subjects_parser", SOURCE_NATS_SUBJECTS_SYNC),
        ("uses_nats", "kg_sync/nats_env", SOURCE_KG_SYNC),
        # kg_sync строит discovered_by динамически (`kg_sync/{source}`).
        ("uses_db", "kg_sync/some_new_heuristic", SOURCE_KG_SYNC),
    ],
)
def test_resolve_edge_sources_attribution(kind, discovered_by, expected):
    assert resolve_edge_sources(kind, discovered_by) == (expected,)


def test_unmapped_kind_resolves_to_nothing():
    """Fail-closed: новый kind не сопоставлен ни одному источнику."""
    assert resolve_edge_sources("brand_new_kind", None) == ()
    assert edge_block_reason("brand_new_kind", None, {}) == REASON_UNMAPPED_KIND


# ── источник упал / вернул ноль → его kind не децаится ───────────────────────


def test_stats_empty_fetch_blocks_decay_despite_fresh_edges(db, caplog):
    """ГЛАВНЫЙ сценарий. `services_fetched=0` (kubectl-таймаут), но в графе
    ещё лежат свежие рёбра прошлого тика — по данным источник выглядит живым.
    Ловит деградацию ТОЛЬКО per-cycle stats-отчёт: старые serves_traffic не
    удаляются и не гаснут, а здоровый `calls` децаится как раньше."""
    _healthy_calls_baseline(db)
    _edge(db, "c-old", "calls", "kg_sync/env_vars", age_days=40)
    # Свежее ребро прошлого (успешного) тика — оно и маскировало сбой.
    _edge(db, "st-fresh", "serves_traffic", "k8s_topology_resources/service", 0)
    _edge(db, "st-old", "serves_traffic", "k8s_topology_resources/service", 40)
    _edge(db, "st-mid", "serves_traffic", "k8s_topology_resources/service", 10)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})
    record_source_run(
        SOURCE_TOPOLOGY_SERVICES,
        {"services_fetched": 0, "errors": 0, "edges_serves_traffic": 0},
    )

    with caplog.at_level(logging.WARNING, logger=KG_SYNC_LOGGER):
        stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert stats["skipped_decay"] == 0            # общий deadman не сработал
    # serves_traffic защищён целиком: ни DELETE, ни soft-mark.
    assert "st-old" in _names(db)
    assert "st-mid" in _names(db)
    assert _inactive(db, "st-mid") is False
    # ...а здоровый kind децаится как раньше.
    assert "c-old" not in _names(db)
    assert stats["deleted"] == 1
    assert stats["blocked_kinds"] == {REASON_EMPTY_FETCH: {"serves_traffic": 2}}

    # Пропуск ГРОМКО залогирован: молчаливая эрозия — исходная беда.
    warning = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "edge_decay_source_unhealthy" in warning
    assert REASON_EMPTY_FETCH in warning
    assert "serves_traffic" in warning


def test_stats_fetch_errors_block_decay(db):
    """Синк отчитался с errors>0 → его kind выведен из decay."""
    _healthy_calls_baseline(db)
    _edge(db, "ing-old", "calls", "kg_sync/ingress", age_days=40)
    _edge(db, "ing-fresh", "calls", "kg_sync/ingress", age_days=0)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})
    record_source_run(
        SOURCE_INGRESS_SYNC, {"ingresses_fetched": 12, "errors": 4},
    )

    stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "ing-old" in _names(db)
    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {REASON_FETCH_ERRORS: {"calls": 1}}


def test_stats_sync_failed_blocks_decay(db):
    """nats_subjects_sync рапортует аварию словарём `{"error": ...}` —
    сбой git-клона неотличим от «в монорепе больше нет NATS-вызовов»."""
    _healthy_calls_baseline(db)
    _edge(db, "subj-fresh", "uses_nats", "kg_sync/nats_subjects_parser", 0)
    _edge(db, "subj-old", "uses_nats", "kg_sync/nats_subjects_parser", 40)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})
    record_source_run(
        SOURCE_NATS_SUBJECTS_SYNC, {"error": "git_failed", "files_scanned": 0},
    )

    stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "subj-old" in _names(db)
    assert stats["blocked_kinds"] == {REASON_SYNC_FAILED: {"uses_nats": 1}}


def test_silent_source_blocks_decay_without_any_report(db):
    """Отчёта нет вовсе (синк живёт в другом процессе) — работает фоллбэк по
    данным: источник, не освеживший ни одного ребра за окно, децаить нельзя."""
    _healthy_calls_baseline(db)
    _edge(db, "st-old", "serves_traffic", "k8s_topology_resources/service", 40)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})

    stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "st-old" in _names(db)
    assert stats["blocked_kinds"] == {REASON_NO_RECENT_REFRESH: {"serves_traffic": 1}}


def test_healthy_report_does_not_override_stale_data(db):
    """Успешный прогон НЕ легализует удаление рёбер, которых он не касался:
    сигналы независимы, блокирует худший (сохраняем предохранитель master-а)."""
    _healthy_calls_baseline(db)
    _edge(db, "st-old", "serves_traffic", "k8s_topology_resources/service", 40)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})
    # Синк отработал чисто, но ни одного serves_traffic не освежил.
    record_source_run(
        SOURCE_TOPOLOGY_SERVICES,
        {"services_fetched": 120, "errors": 0, "edges_serves_traffic": 0},
    )

    stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "st-old" in _names(db)
    assert stats["deleted"] == 0


# ── здоровый источник децаится как раньше ────────────────────────────────────


def test_healthy_source_decays_as_before(db):
    """Контроль: оба сигнала чисты → старое удаляется, среднее гаснет."""
    _healthy_calls_baseline(db)
    _edge(db, "st-fresh", "serves_traffic", "k8s_topology_resources/service", 0)
    _edge(db, "st-old", "serves_traffic", "k8s_topology_resources/service", 40)
    _edge(db, "st-mid", "serves_traffic", "k8s_topology_resources/service", 10)
    db.commit()

    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})
    record_source_run(
        SOURCE_TOPOLOGY_SERVICES,
        {"services_fetched": 120, "errors": 0, "edges_serves_traffic": 118},
    )

    stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "st-old" not in _names(db)
    assert stats["deleted"] == 1
    assert _inactive(db, "st-mid") is True
    assert stats["marked_inactive"] == 1
    assert stats["blocked_kinds"] == {}


def test_existing_deadman_and_threshold_still_apply(db):
    """Существующие предохранители на месте: fetch-ошибки самого kg_sync и
    25%-cap по-прежнему отменяют весь проход."""
    _healthy_calls_baseline(db)
    _edge(db, "c-old", "calls", "kg_sync/env_vars", age_days=40)
    db.commit()
    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})

    stats = _decay_stale_edges(db, has_fetch_errors=True)
    assert stats["skipped_decay"] == 1
    assert "c-old" in _names(db)

    # 25%-cap: 3 старых из 6 = 50% > 25%.
    for i in range(2):
        _edge(db, f"c-old-{i}", "calls", "kg_sync/env_vars", age_days=40)
    db.commit()
    stats = _decay_stale_edges(db, has_fetch_errors=False)
    assert stats["skipped_decay"] == 1
    assert db.query(ServiceEdge).count() == 6


def test_blocked_kinds_are_logged_even_when_global_deadman_fires(db, caplog):
    """Общий deadman не должен глушить per-kind сигнал: если класс рёбер уже
    под защитой сломанного синка, это видно и на раннем выходе."""
    _healthy_calls_baseline(db)
    _edge(db, "st-old", "serves_traffic", "k8s_topology_resources/service", 40)
    db.commit()
    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})

    with caplog.at_level(logging.WARNING, logger=KG_SYNC_LOGGER):
        stats = _decay_stale_edges(db, has_fetch_errors=True)

    assert stats["skipped_decay"] == 1
    assert stats["blocked_kinds"] == {REASON_NO_RECENT_REFRESH: {"serves_traffic": 1}}
    warning = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "edge_decay_skipped" in warning          # общий deadman
    assert "edge_decay_source_unhealthy" in warning  # и per-kind причина


# ── fail-closed: несопоставленный kind ───────────────────────────────────────


def test_unmapped_kind_is_never_decayed_and_is_logged(db, caplog):
    """Новый kind, не заведённый в карте, НЕ эродирует молча: decay его не
    трогает, а в логе висит громкий unmapped_kind с указанием, что чинить."""
    _healthy_calls_baseline(db)
    _edge(db, "new-old", "brand_new_kind", "some_future_sync/thing", 40)
    _edge(db, "new-mid", "brand_new_kind", "some_future_sync/thing", 10)
    db.commit()
    record_source_run(SOURCE_KG_SYNC, {"namespaces": 3, "errors": 0})

    with caplog.at_level(logging.WARNING, logger=KG_SYNC_LOGGER):
        stats = _decay_stale_edges(db, has_fetch_errors=False)

    assert "new-old" in _names(db)
    assert _inactive(db, "new-mid") is False
    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {REASON_UNMAPPED_KIND: {"brand_new_kind": 2}}

    warning = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "edge_decay_unmapped_kind" in warning
    assert "brand_new_kind" in warning
    assert "EDGE_KIND_FRESHNESS_SOURCES" in warning


# ── здоровье источников как таковое ──────────────────────────────────────────


def test_source_without_edges_is_not_reported_unhealthy(db):
    """Источник, у которого в графе нет рёбер, защищать нечего — он не
    должен светиться в stale_sources и шуметь в логах."""
    _healthy_calls_baseline(db)
    db.commit()

    bad = unhealthy_sources(db)

    assert SOURCE_TOPOLOGY_SERVICES not in bad
    assert SOURCE_NATS_SUBJECTS_SYNC not in bad


# ── тот же guard для kg_volume_edges (storage-слой) ──────────────────────────
#
# Decay volume-рёбер (`k8s_storage_sync.decay_volume_edges`) появился в третьей
# волне ревью: до него удалённый PVC жил в графе вечно. Чистка обязана
# опираться на здоровье СВОЕГО среза — pod-лист cluster-wide самый тяжёлый
# fetch во всём KG, и именно он таймаутит первым.


def test_every_live_volume_edge_kind_is_mapped_to_a_source():
    """Карта покрывает все kind'ы, живущие в kg_volume_edges."""
    from app.knowledge_graph.contract import EDGE_KINDS

    live = {
        name for name, spec in EDGE_KINDS.items()
        if spec.get("table") == "kg_volume_edges" and spec.get("status") == "active"
    }
    assert live, "контракт не отдал ни одного live volume-kind — проверь фильтр"
    assert live <= set(VOLUME_EDGE_KIND_FRESHNESS_SOURCES)


@pytest.mark.parametrize(
    "kind,discovered_by,expected",
    [
        ("uses_volume", "k8s_storage/pod_volumes", SOURCE_STORAGE_PODS),
        ("bound_to", "k8s_storage/pvc_spec", SOURCE_STORAGE_PVCS),
        # Оба kind'а однозначны — атрибутируются и без discovered_by.
        ("uses_volume", None, SOURCE_STORAGE_PODS),
        ("bound_to", None, SOURCE_STORAGE_PVCS),
    ],
)
def test_resolve_volume_edge_sources_attribution(kind, discovered_by, expected):
    assert resolve_volume_edge_sources(kind, discovered_by) == (expected,)


def test_volume_and_service_edge_maps_do_not_leak_into_each_other(db):
    """Таблицы рёбер судятся по СВОЕМУ инвентарю: kind одной не
    атрибутируется картой другой (иначе сбой одного среза морозил бы decay
    в чужой таблице)."""
    assert resolve_edge_sources("uses_volume", None) == ()
    assert resolve_volume_edge_sources("calls", "kg_sync/env_vars") == ()
    # В графе только service-рёбра → storage-источники не в инвентаре.
    _healthy_calls_baseline(db)
    db.commit()
    assert unhealthy_volume_sources(db) == {}


def test_unmapped_volume_kind_is_fail_closed():
    assert volume_edge_block_reason("brand_new_volume_kind", None, {}) == (
        REASON_UNMAPPED_KIND
    )


def test_volume_source_health_is_per_slice(db):
    """Сбой pod-среза не должен блокировать decay `bound_to` и наоборот."""
    svc = upsert_service(db, "squad-1", "src")
    db.flush()
    now = datetime.utcnow()
    db.add(VolumeEdge(
        src_kind="service", src_id=svc.id, dst_kind="pvc", dst_id=1,
        kind="uses_volume", discovered_by="k8s_storage/pod_volumes",
        last_seen_at=now,
    ))
    db.add(VolumeEdge(
        src_kind="pvc", src_id=1, dst_kind="pv", dst_id=2,
        kind="bound_to", discovered_by="k8s_storage/pvc_spec",
        last_seen_at=now,
    ))
    db.commit()

    record_source_run(SOURCE_STORAGE_PODS, {"pods_scanned": 0, "errors": 1})
    record_source_run(SOURCE_STORAGE_PVCS, {"pvcs_fetched": 42, "errors": 0})

    bad = unhealthy_volume_sources(db)

    assert bad == {SOURCE_STORAGE_PODS: REASON_FETCH_ERRORS}
    assert volume_edge_block_reason(
        "uses_volume", "k8s_storage/pod_volumes", bad,
    ) == REASON_FETCH_ERRORS
    assert volume_edge_block_reason(
        "bound_to", "k8s_storage/pvc_spec", bad,
    ) is None
