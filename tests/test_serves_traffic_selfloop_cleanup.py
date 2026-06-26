"""Тесты чистки serves_traffic self-loop рёбер (src_id == dst_id)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.serves_traffic_selfloop_cleanup import (
    delete_serves_traffic_self_loops,
)
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


def _svc(db, name, ns="prod-shared"):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _edge(db, src, dst, kind="serves_traffic"):
    e = ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind)
    db.add(e)
    db.flush()
    return e


def test_dry_run_reports_without_deleting(db):
    a = _svc(db, "auth")
    _edge(db, a, a)  # self-loop
    db.commit()
    stats = delete_serves_traffic_self_loops(db, apply=False)
    assert stats["self_loops_found"] == 1
    assert stats["deleted"] == 0
    assert stats["applied"] is False
    assert db.query(ServiceEdge).count() == 1


def test_apply_deletes_only_self_loops(db):
    a = _svc(db, "auth")
    b = _svc(db, "town")
    _edge(db, a, a)   # self-loop → удалить
    _edge(db, a, b)   # cross-node → оставить
    db.commit()
    stats = delete_serves_traffic_self_loops(db, apply=True)
    assert stats["self_loops_found"] == 1
    assert stats["deleted"] == 1
    assert stats["applied"] is True
    remaining = db.query(ServiceEdge).all()
    assert len(remaining) == 1
    assert remaining[0].src_id != remaining[0].dst_id


def test_apply_ignores_self_loop_of_other_kind(db):
    a = _svc(db, "auth")
    _edge(db, a, a, kind="calls")  # self-loop, но не serves_traffic
    db.commit()
    stats = delete_serves_traffic_self_loops(db, apply=True)
    assert stats["self_loops_found"] == 0
    assert stats["deleted"] == 0
    assert db.query(ServiceEdge).count() == 1


def test_idempotent_second_run_noop(db):
    a = _svc(db, "auth")
    _edge(db, a, a)
    db.commit()
    delete_serves_traffic_self_loops(db, apply=True)
    again = delete_serves_traffic_self_loops(db, apply=True)
    assert again["self_loops_found"] == 0
    assert again["deleted"] == 0
    assert again["applied"] is False
