"""Тесты check_graph_integrity — regression-watch инвариантов графа (#185/#189/#190)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import Service, ServiceEdge
from app.knowledge_graph.self_health import check_graph_integrity


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


def _svc(db, name, ns="prod-shared"):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _edge(db, src_id, dst_id, kind="serves_traffic"):
    e = ServiceEdge(src_id=src_id, dst_id=dst_id, kind=kind)
    db.add(e)
    db.flush()
    return e


def test_clean_graph_ok(db):
    a = _svc(db, "auth-svc")
    b = _svc(db, "auth-app")
    _edge(db, a.id, b.id)                 # cross-node, валидно
    _svc(db, "db:postgres:town", "prod-shared")  # одна копия db-узла
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "ok"
    assert r.detail["db_phantom_dup_names"] == 0
    assert r.detail["self_loops_any"] == 0
    assert r.detail["dangling_edges"] == 0


def test_phantom_db_dup_fails(db):
    # один db-узел в двух namespace → фантом-дубль (регрессия #185/#189)
    _svc(db, "db:postgres:town", "squad-1")
    _svc(db, "db:postgres:town", "squad-2")
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "fail"
    assert r.detail["db_phantom_dup_names"] == 1


def test_serves_traffic_self_loop_fails(db):
    a = _svc(db, "auth")
    _edge(db, a.id, a.id, kind="serves_traffic")  # петля (регрессия #190)
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "fail"
    assert r.detail["self_loops_any"] == 1
    assert r.detail["serves_traffic_self_loops"] == 1


def test_few_dangling_edges_warn(db):
    a = _svc(db, "auth")
    _edge(db, a.id, 999999, kind="calls")  # dst не существует → висячее
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "warn"
    assert r.detail["dangling_edges"] == 1


def test_mass_dangling_edges_fail(db):
    a = _svc(db, "auth")
    for i in range(60):  # > порога 50 → fail
        _edge(db, a.id, 900000 + i, kind="calls")
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "fail"
    assert r.detail["dangling_edges"] == 60
