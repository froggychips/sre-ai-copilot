"""Этап 0 роадмапа: проверки правдивости самих данных.

Три вопроса, на которые до сих пор не отвечал ни один сигнал:
узлы графа свежи? все источники отработали? какие колонки-измерения пусты
целиком? Последнее — самый дорогой класс ошибок здесь: пустая колонка
выглядит в выдаче ровно так же, как измеренная.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import edge_decay_guard
from app.knowledge_graph.edge_decay_guard import (ALL_EDGE_SOURCES,
                                                  SOURCE_KG_SYNC,
                                                  record_source_run)
from app.knowledge_graph.schema import (Namespace, Service, ServiceHealth,
                                        StorageVolume)
from app.knowledge_graph.self_health import (check_node_freshness,
                                             check_silent_gaps,
                                             check_source_coverage)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean_reports():
    edge_decay_guard.reset_source_reports()
    yield
    edge_decay_guard.reset_source_reports()


def _ns(db, name="prod-shared", state="active"):
    db.add(Namespace(namespace=name, state=state))
    db.flush()


def _node(db, name, kind, age_hours, ns="prod-shared"):
    db.add(Service(name=name, namespace=ns, node_kind=kind,
                   updated_at=datetime.utcnow() - timedelta(hours=age_hours)))
    db.flush()


# ── свежесть узлов ─────────────────────────────────────────────────────────


def test_stale_node_kind_raises_warn(db):
    """Больше 30% устаревших узлов одного типа — сбой источника, не хвост.

    Замер 17.09.2026: ingress-узлов 334, из них 194 не обновлялись сутки.
    Рёбра `routes_to` при этом свежи, поэтому `check_edges_freshness`
    молчала — узлы в графе больше чем наполовину описывали вчерашний
    кластер, и не было сигнала, который бы это показал.
    """
    _ns(db)
    for i in range(6):
        _node(db, f"ing-old-{i}", "ingress", age_hours=48)
    for i in range(4):
        _node(db, f"ing-new-{i}", "ingress", age_hours=1)
    db.commit()

    r = check_node_freshness(db)
    assert r.detail["by_node_kind"]["ingress"]["stale"] == 6
    assert r.detail["worst_kind"] == "ingress"
    assert r.status == "warn"


def test_fresh_nodes_are_ok(db):
    _ns(db)
    for i in range(5):
        _node(db, f"svc-{i}", "service", age_hours=1)
    db.commit()

    r = check_node_freshness(db)
    assert r.detail["by_node_kind"]["service"]["stale"] == 0
    assert r.status == "ok"


def test_dead_namespace_nodes_are_not_counted(db):
    """У снесённого окружения узлы обязаны устаревать.

    Мерить по ним работу синка — та же ошибка, которую модуль уже совершал
    с рёбрами: проверка считала скорость retention вместо сбора данных.
    """
    _ns(db, "prod-shared", "active")
    _ns(db, "squad-99-shared", "missing")
    _node(db, "live", "service", age_hours=1)
    for i in range(9):
        _node(db, f"dead-{i}", "service", age_hours=200, ns="squad-99-shared")
    db.commit()

    r = check_node_freshness(db)
    assert r.detail["by_node_kind"]["service"]["total"] == 1
    assert r.status == "ok"


# ── покрытие источников ────────────────────────────────────────────────────


def test_silent_source_is_listed_but_does_not_raise_status(db):
    """Молчание источника видно в detail, но статуса НЕ поднимает.

    `_REPORTS` живёт в памяти процесса, а celery крутит несколько воркеров
    с рециклом — одна проверка физически видит лишь часть источников.
    Поднимай silent статус, warn горел бы всегда и через сутки стал
    залипшим CopilotSelfHealthWarnStuck: ровно тот шум, ради устранения
    которого Этап 0 и делается. Молчание — «не знаю», а не «плохо».
    """
    record_source_run(SOURCE_KG_SYNC, {"services_fetched": 10, "errors": 0})

    r = check_source_coverage(db)
    assert r.detail["sources_reported"] == 1
    assert len(r.detail["silent"]) == len(ALL_EDGE_SOURCES) - 1
    assert r.status == "ok"


def test_all_sources_reported_is_ok(db):
    for source in ALL_EDGE_SOURCES:
        record_source_run(source, {"errors": 0})

    r = check_source_coverage(db)
    assert r.detail["silent"] == []
    assert r.detail["coverage_pct"] == 100.0
    assert r.status == "ok"


def test_failed_source_is_unhealthy(db):
    for source in ALL_EDGE_SOURCES:
        record_source_run(source, {"errors": 0})
    record_source_run(SOURCE_KG_SYNC, {"error": "kubectl timeout"})

    r = check_source_coverage(db)
    assert SOURCE_KG_SYNC in r.detail["unhealthy"]
    assert r.status == "warn"


# ── пустые колонки-измерения ───────────────────────────────────────────────


def test_empty_measurement_column_is_surfaced(db):
    """Колонка без единого значения перестаёт выглядеть измеренной.

    `kg_storage_volumes.disk_pct` пуст во всех 12 088 строках: kubelet не
    собирает volume stats для local-path, это не CSI. В выдаче поле
    выглядело так же, как заполненное.
    """
    for i in range(3):
        db.add(StorageVolume(kind="pvc", namespace="prod-shared",
                             name=f"pvc-{i}", disk_pct=None))
    db.commit()

    r = check_silent_gaps(db)
    cols = {g["column"] for g in r.detail["empty_measurement_columns"]}
    assert "disk_pct" in cols
    assert r.status == "warn"


def test_filled_column_is_not_a_gap(db):
    """Хотя бы одно значение — уже не пробел."""
    db.add(StorageVolume(kind="pvc", namespace="prod-shared",
                         name="pvc-measured", disk_pct=42.0))
    svc = Service(name="measured-svc", namespace="prod-shared")
    db.add(svc)
    db.flush()
    db.add(ServiceHealth(service_id=svc.id, ts=datetime.utcnow(),
                         http_5xx_rate=0.1, p95_latency_ms=12.0))
    db.commit()

    r = check_silent_gaps(db)
    cols = {g["column"] for g in r.detail["empty_measurement_columns"]}
    assert "disk_pct" not in cols
    assert "http_5xx_rate" not in cols


def test_empty_table_is_not_a_gap(db):
    """Пустая таблица — не пробел в измерении, а отсутствие объектов."""
    r = check_silent_gaps(db)
    assert r.detail["empty_measurement_columns"] == []
    assert r.status == "ok"



def test_node_freshness_excludes_health_touched_nodes(db):
    """Узел, которого коснулся health-пересчёт, не считается свежим.

    `Service.updated_at` имеет onupdate=utcnow, а kg_health_recompute
    каждые 20 минут пишет health_score всем non-synthetic сервисам. Замер
    17.09.2026: у service health_computed_at стоял на текущей минуте, у
    workload — месячной давности, у ingress его нет вовсе. Без исключения
    таких узлов проверка показывала бы вечные 0,9% даже при полностью
    вставшем синке топологии — то есть врала бы зелёным.
    """
    _ns(db)
    stale_ts = datetime.utcnow() - timedelta(hours=48)
    # Узел, у которого последнее касание — от health-пересчёта.
    db.add(Service(name="health-touched", namespace="prod-shared",
                   node_kind="service", updated_at=stale_ts,
                   health_computed_at=stale_ts))
    # Узел, которого health не касался вовсе.
    db.add(Service(name="topology-only", namespace="prod-shared",
                   node_kind="service", updated_at=stale_ts,
                   health_computed_at=None))
    db.commit()

    r = check_node_freshness(db)
    svc = r.detail["by_node_kind"]["service"]
    assert svc["total"] == 2
    assert svc["not_measurable"] == 1
    assert svc["measurable"] == 1
    assert svc["stale"] == 1


def test_silent_gaps_uses_recent_window_for_timeseries(db):
    """Регрессия сбора видна, даже если значение когда-то было.

    Раньше счёт шёл по всей истории: одно непустое значение за всё время
    навсегда прятало бы то, что сбор сломался вчера. Плюс полный скан
    time-series таблицы дорожает вместе с ней.
    """
    svc = Service(name="svc", namespace="prod-shared")
    db.add(svc)
    db.flush()
    # Древняя точка с измерением — за пределами окна.
    db.add(ServiceHealth(service_id=svc.id,
                         ts=datetime.utcnow() - timedelta(days=30),
                         http_5xx_rate=0.5, p95_latency_ms=10.0))
    # Свежие точки без измерения — сбор сломался.
    for h in (1, 2, 3):
        db.add(ServiceHealth(service_id=svc.id,
                             ts=datetime.utcnow() - timedelta(hours=h),
                             http_5xx_rate=None, p95_latency_ms=None))
    db.commit()

    r = check_silent_gaps(db)
    cols = {g["column"] for g in r.detail["empty_measurement_columns"]}
    assert "http_5xx_rate" in cols
    assert "p95_latency_ms" in cols
    assert r.status == "warn"
