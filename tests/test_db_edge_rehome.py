"""Перевес рёбер `uses_db` с баз удалённых окружений на живые.

Контекст в docstring `app.knowledge_graph.db_edge_rehome`: отключённый
`phantom_db_cleanup` схлопывал разные физические базы в одну, выбирая
канонической ту, что в лексикографически минимальном namespace. На проде
20.08.2026 это дало 3676 рёбер «живой сервис → база удалённого
preprod-kingdom1».
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.db_edge_rehome import rehome_db_edges
from app.knowledge_graph.schema import Namespace, Service, ServiceEdge


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


def _ns(db, name, state="active"):
    db.add(Namespace(namespace=name, state=state))
    db.flush()


def _svc(db, name, ns):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _edge(db, src, dst, kind="uses_db", weight=1):
    e = ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind, weight=weight)
    db.add(e)
    db.flush()
    return e


def _scene(db):
    """Прод-сервис, ссылающийся на базу удалённого окружения, и живая база."""
    _ns(db, "prod-shared", "active")
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_db = _svc(db, "db:postgres:town", "preprod-kingdom1")
    live_db = _svc(db, "db:postgres:town", "prod-shared")
    client = _svc(db, "town-service", "prod-kingdom1")
    edge = _edge(db, client, dead_db)
    db.commit()
    return client, dead_db, live_db, edge


def test_dry_run_writes_nothing(db):
    client, dead_db, live_db, edge = _scene(db)
    stats = rehome_db_edges(db, apply=False)
    assert stats["applied"] is False
    assert stats["stale_edges"] == 1
    assert stats["repointed"] == 1      # приёмник найден
    assert stats["no_target"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id, "dry-run изменил граф"


def test_edge_moves_to_shared_of_source_realm(db):
    """Ребро уходит в `<realm>-shared` окружения ИСТОЧНИКА, а не куда попало."""
    client, dead_db, live_db, edge = _scene(db)
    stats = rehome_db_edges(db, apply=True)
    assert stats["applied"] is True
    assert stats["repointed"] == 1
    db.refresh(edge)
    assert edge.dst_id == live_db.id


def test_second_run_is_a_noop(db):
    """Идемпотентность: повторный прогон на исправленном графе ничего не делает."""
    _scene(db)
    rehome_db_edges(db, apply=True)
    again = rehome_db_edges(db, apply=True)
    assert again["stale_edges"] == 0
    assert again["repointed"] == 0


def test_merges_into_existing_correct_edge_keeping_max_weight(db):
    """Если правильное ребро уже создано синком — сливаем, вес не понижаем."""
    client, dead_db, live_db, stale = _scene(db)
    correct = _edge(db, client, live_db, weight=3)
    stale.weight = 7
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["merged"] == 1
    assert stats["repointed"] == 0
    db.refresh(correct)
    assert correct.weight == 7, "вес понизился при слиянии"
    assert db.query(ServiceEdge).filter(ServiceEdge.id == stale.id).first() is None


def test_edge_without_target_is_left_alone(db):
    """Нет узла-приёмника — ребро не трогаем и не выдумываем узел.

    Выдуманный узел хуже устаревшего: устаревший видит `graph_integrity`,
    выдуманный неотличим от настоящего.
    """
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_db = _svc(db, "db:postgres:lonely", "preprod-kingdom1")
    client = _svc(db, "town-service", "prod-kingdom1")
    edge = _edge(db, client, dead_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["no_target"] == 1
    assert stats["repointed"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id


def test_dead_to_dead_edges_are_not_touched(db):
    """Снесённый сквад ссылается на свою же базу — законная история.

    Её убирает retention в `namespace_lifecycle`, а не этот модуль: перенос
    сделал бы вид, будто мёртвое окружение ходит в живую базу.
    """
    _ns(db, "squad-23-shared", "missing")
    _ns(db, "prod-shared", "active")
    _svc(db, "db:postgres:town", "prod-shared")
    dead_db = _svc(db, "db:postgres:town", "squad-23-shared")
    dead_client = _svc(db, "town-service", "squad-23-shared")
    edge = _edge(db, dead_client, dead_db)
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_db.id


def test_non_db_edges_are_out_of_scope(db):
    """Модуль трогает только `db:%`-узлы и только виды рёбер из DB_EDGE_KINDS."""
    _ns(db, "prod-kingdom1", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_svc = _svc(db, "town-service", "preprod-kingdom1")
    client = _svc(db, "gateway", "prod-kingdom1")
    edge = _edge(db, client, dead_svc, kind="calls")
    db.commit()

    stats = rehome_db_edges(db, apply=True)
    assert stats["stale_edges"] == 0
    db.refresh(edge)
    assert edge.dst_id == dead_svc.id


def test_retired_collapse_refuses_to_run():
    """Старый backfill обязан падать с объяснением, а не тихо портить граф.

    Он доступен из CLI с `--apply`; его dry-run тоже отключён, потому что
    отчёт называл дублями 56 законных узлов и подталкивал применить перенос.
    """
    from app.knowledge_graph.phantom_db_cleanup import (
        PhantomDbCleanupRetired, collapse_phantom_db_nodes)

    for apply in (False, True):
        with pytest.raises(PhantomDbCleanupRetired) as exc:
            collapse_phantom_db_nodes(None, apply=apply)  # type: ignore[arg-type]
        assert "db_edge_rehome" in str(exc.value)
