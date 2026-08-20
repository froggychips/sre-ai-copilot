"""Тесты backfill-схлопывания фантомных db-узлов (C2) — ИСТОРИЧЕСКОГО.

Публичная `collapse_phantom_db_nodes` отключена: её правило «канонический
узел = в лексикографически минимальном namespace» сводило разные физические
базы разных окружений в одну (замер 20.08.2026: `db:postgres:message` в 56
namespace — 56 разных баз, каждая обслуживает своё окружение). Отказ функции
проверяется в `test_db_edge_rehome.py`, разгребает последствия
`app.knowledge_graph.db_edge_rehome`.

Эти тесты переключены на сохранённое тело `_collapse_phantom_db_nodes_historical`
и оставлены намеренно: они документируют, ЧТО именно делал backfill, а без
этого нельзя понять, откуда в графе взялись 3676 рёбер «прод → база
удалённого препрода». Тот же класс ошибки легко повторить в следующем
дедупликаторе — пусть будет видно, как он выглядит.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.phantom_db_cleanup import \
    _collapse_phantom_db_nodes_historical as collapse_phantom_db_nodes
from app.knowledge_graph.schema import Service, ServiceEdge


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


def _svc(db, name, ns, synthetic=False):
    s = Service(name=name, namespace=ns, synthetic=synthetic)
    db.add(s)
    db.flush()
    return s


def _edge(db, src, dst, kind="uses_db", weight=1):
    e = ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind, weight=weight)
    db.add(e)
    db.flush()
    return e


@pytest.fixture
def graph(db):
    """16-БД-в-N-копий в миниатюре: одна БД в 3 ns + 2 app-сервиса."""
    svc_a = _svc(db, "town-service", "squad-1")
    svc_b = _svc(db, "town-service", "squad-2")
    # db:postgres:town в 3 ns → канонический = lexicographically min = prod-shared
    canon = _svc(db, "db:postgres:town", "prod-shared", synthetic=True)
    dup1 = _svc(db, "db:postgres:town", "squad-1", synthetic=True)
    dup2 = _svc(db, "db:postgres:town", "squad-2", synthetic=True)
    # svc_a уже ссылается на канонический (weight=2) И на дубль (weight=5) → merge
    _edge(db, svc_a, canon, weight=2)
    _edge(db, svc_a, dup1, weight=5)
    # svc_b ссылается только на дубль → чистый repoint
    _edge(db, svc_b, dup2, weight=3)
    db.commit()
    return {"svc_a": svc_a, "svc_b": svc_b, "canon": canon}


def test_dry_run_reports_without_writing(db, graph):
    stats = collapse_phantom_db_nodes(db, apply=False)
    assert stats["distinct_db_names"] == 1
    assert stats["total_db_nodes"] == 3
    assert stats["duplicate_names"] == 1
    assert stats["nodes_to_delete"] == 2
    assert stats["applied"] is False
    assert stats["nodes_deleted"] == 0
    # Ничего не удалено.
    assert db.query(Service).filter(Service.name == "db:postgres:town").count() == 3


def test_apply_collapses_to_canonical(db, graph):
    canon = graph["canon"]
    stats = collapse_phantom_db_nodes(db, apply=True)
    assert stats["applied"] is True
    assert stats["nodes_deleted"] == 2
    assert stats["edges_merged"] == 1      # svc_a→dup1 слит в svc_a→canon
    assert stats["edges_repointed"] == 1   # svc_b→dup2 перенаправлен на canon

    db_nodes = db.query(Service).filter(Service.name == "db:postgres:town").all()
    assert len(db_nodes) == 1
    assert db_nodes[0].id == canon.id
    assert db_nodes[0].namespace == "prod-shared"


def test_apply_merges_weight_greatest(db, graph):
    canon = graph["canon"]
    svc_a = graph["svc_a"]
    collapse_phantom_db_nodes(db, apply=True)
    # svc_a → canon: было weight=2, дубль был weight=5 → max=5
    e = (db.query(ServiceEdge)
         .filter(ServiceEdge.src_id == svc_a.id, ServiceEdge.dst_id == canon.id)
         .one())
    assert e.weight == 5
    # Все рёбра теперь указывают на canon, дубль-узлов нет.
    assert db.query(ServiceEdge).filter(ServiceEdge.dst_id == canon.id).count() == 2


def test_real_services_untouched(db, graph):
    collapse_phantom_db_nodes(db, apply=True)
    assert db.query(Service).filter(Service.name == "town-service").count() == 2


def test_idempotent_second_run_is_noop(db, graph):
    collapse_phantom_db_nodes(db, apply=True)
    again = collapse_phantom_db_nodes(db, apply=True)
    assert again["nodes_to_delete"] == 0
    assert again["nodes_deleted"] == 0
    assert again["duplicate_names"] == 0


def test_single_copy_not_touched(db):
    """db-узел в единственном экземпляре не считается дублем."""
    _svc(db, "db:postgres:solo", "prod-shared", synthetic=True)
    db.commit()
    stats = collapse_phantom_db_nodes(db, apply=True)
    assert stats["nodes_to_delete"] == 0
    assert db.query(Service).filter(Service.name == "db:postgres:solo").count() == 1
