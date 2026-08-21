"""Рёбра прежней инкарнации не должны оживать вместе с именем namespace.

Шаг B2 намеренно только наблюдал: пересоздание стенда фиксировалось, но ни
узлы, ни рёбра не трогались — «сначала неделя наблюдения, потом действие».
Неделя показала, что происходит без действия.

Замер 21.08.2026: 1033 ребра в 22 пересозданных namespace держались с
прежнего воплощения — 636 `uses_db`, 388 `routes_to`, 374 `uses_nats`.
Показательный случай: `squad-10-kingdom2` пересоздали (incarnation 3), и его
рёбра от 08.08 на базы удалённого `preprod-kingdom1` снова стали
утверждением о работающем окружении. `db_edge_rehome` их подчищал, но это
лечение симптома: `routes_to` и `uses_nats` того же происхождения не
подчищает никто.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.namespace_lifecycle import (
    NS_STATE_ACTIVE, NS_STATE_MISSING, purge_stale_edges_after_reincarnation)
from app.knowledge_graph.schema import Namespace, Service, ServiceEdge

NOW = datetime(2026, 8, 21, 12, 0, 0)


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


def _ns(db, name, *, incarnation=2, born_hours_ago=5, state=NS_STATE_ACTIVE):
    db.add(Namespace(
        namespace=name, state=state, incarnation=incarnation,
        first_seen_at=NOW - timedelta(hours=born_hours_ago),
        last_seen_at=NOW,
    ))
    db.flush()


def _svc(db, name, ns):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _edge(db, src, dst, *, seen_hours_ago, kind="uses_db"):
    e = ServiceEdge(src_id=src.id, dst_id=dst.id, kind=kind,
                    last_seen_at=NOW - timedelta(hours=seen_hours_ago))
    db.add(e)
    db.flush()
    return e


def test_edge_not_confirmed_after_rebirth_is_deleted(db):
    """Ребро старше нового воплощения — факт о стенде, которого уже нет."""
    _ns(db, "squad-10-shared", incarnation=3, born_hours_ago=5)
    a = _svc(db, "town-service", "squad-10-shared")
    b = _svc(db, "db:postgres:town", "squad-10-shared")
    stale_id = _edge(db, a, b, seen_hours_ago=100).id   # подтверждено до рождения
    fresh_id = _edge(db, b, a, seen_hours_ago=1).id     # подтверждено после
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["edges_deleted"] == 1
    # id, а не объекты: удалённая строка при обращении к атрибуту даёт
    # ObjectDeletedError — SQLAlchemy пытается перечитать то, чего нет.
    assert db.query(ServiceEdge).filter(ServiceEdge.id == stale_id).first() is None
    assert db.query(ServiceEdge).filter(ServiceEdge.id == fresh_id).first() is not None


def test_grace_period_protects_a_just_recreated_namespace(db):
    """Сразу после пересоздания чистить нельзя: синк не успел подтвердить.

    Рёбра подтверждают синки, и самый редкий ходит раз в час. Удаление без
    выдержки снесло бы связи живого стенда — те, что синк ещё не видел.
    """
    _ns(db, "squad-10-shared", incarnation=3, born_hours_ago=1)   # < 2 часов
    a = _svc(db, "town-service", "squad-10-shared")
    b = _svc(db, "db:postgres:town", "squad-10-shared")
    edge = _edge(db, a, b, seen_hours_ago=100)
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["skipped_in_grace"] == 1
    assert stats["edges_deleted"] == 0
    assert db.query(ServiceEdge).filter(ServiceEdge.id == edge.id).first() is not None


def test_guard_blocks_purging_almost_everything(db):
    """Если устарело почти всё — виноват скорее синк, чем стенд.

    Массовое удаление по такой причине в этом проекте уже случалось:
    `edge_decay_guard` заведён ровно после него.
    """
    _ns(db, "squad-10-shared", incarnation=2, born_hours_ago=5)
    a = _svc(db, "town-service", "squad-10-shared")
    for i in range(20):
        b = _svc(db, f"db:postgres:x{i}", "squad-10-shared")
        _edge(db, a, b, seen_hours_ago=100)
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["skipped_guard"] == 1
    assert stats["edges_deleted"] == 0
    assert db.query(ServiceEdge).count() == 20


def test_first_incarnation_is_untouched(db):
    """Стенд не пересоздавали — «прежнего воплощения» не существует."""
    _ns(db, "prod-shared", incarnation=1, born_hours_ago=500)
    a = _svc(db, "town-service", "prod-shared")
    b = _svc(db, "db:postgres:town", "prod-shared")
    edge = _edge(db, a, b, seen_hours_ago=1000)
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["namespaces_checked"] == 0
    assert db.query(ServiceEdge).filter(ServiceEdge.id == edge.id).first() is not None


def test_missing_namespace_is_out_of_scope(db):
    """Исчезнувший стенд — вотчина retention, а не этой чистки."""
    _ns(db, "squad-20-shared", incarnation=2, born_hours_ago=100,
        state=NS_STATE_MISSING)
    a = _svc(db, "town-service", "squad-20-shared")
    b = _svc(db, "db:postgres:town", "squad-20-shared")
    edge = _edge(db, a, b, seen_hours_ago=200)
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["namespaces_checked"] == 0
    assert db.query(ServiceEdge).filter(ServiceEdge.id == edge.id).first() is not None


def test_dry_run_writes_nothing(db):
    _ns(db, "squad-10-shared", incarnation=3, born_hours_ago=5)
    a = _svc(db, "town-service", "squad-10-shared")
    b = _svc(db, "db:postgres:town", "squad-10-shared")
    _edge(db, b, a, seen_hours_ago=1)
    _edge(db, a, b, seen_hours_ago=100)
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=False)
    assert stats["applied"] is False
    assert stats["edges_deleted"] == 1          # посчитали
    assert db.query(ServiceEdge).count() == 2   # но не удалили


def test_all_edge_kinds_are_cleaned_not_just_db(db):
    """routes_to и uses_nats того же происхождения — их не подчищает никто.

    Замер 21.08.2026: 388 `routes_to` и 374 `uses_nats` против 636 `uses_db`.
    Перенос db-рёбер закрывал только треть проблемы.
    """
    _ns(db, "squad-10-shared", incarnation=3, born_hours_ago=5)
    a = _svc(db, "gateway", "squad-10-shared")
    for kind in ("uses_db", "routes_to", "uses_nats", "calls"):
        b = _svc(db, f"peer-{kind}", "squad-10-shared")
        _edge(db, a, b, seen_hours_ago=100, kind=kind)
    # одно свежее, чтобы guard не сработал
    keep = _svc(db, "fresh-peer", "squad-10-shared")
    for i in range(10):
        _edge(db, a, keep, seen_hours_ago=1, kind=f"k{i}")
    db.commit()

    stats = purge_stale_edges_after_reincarnation(db, now=NOW, apply=True)
    assert stats["edges_deleted"] == 4
    remaining = {e.kind for e in db.query(ServiceEdge).all()}
    assert not {"uses_db", "routes_to", "uses_nats", "calls"} & remaining
