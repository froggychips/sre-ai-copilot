"""Пересозданный workload — другой объект, а не тот же самый.

Узел графа опознавался тройкой (namespace, name, node_kind), и этого хватало
ровно до первого пересоздания. Снесённый и заведённый заново Deployment носит
то же имя, и к нему прирастала вся история прежнего: деплои, алерты, health,
рёбра. Для namespace это закрыли 14.08.2026 (`kg_namespaces.k8s_uid` +
`incarnation`), для workload'ов — нет, хотя пересоздают их куда чаще: каждый
`--wipe` сквада, каждая смена селектора, каждый helm uninstall/install.

Здесь проверяется поведение на sqlite-пути (`_upsert_service_fallback`).
PG-путь считает инкремент тем же условием, но в SQL — см. `CASE` в
`_upsert_service_pg`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service


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


def _upsert(db, uid=None, name="town-service", ns="squad-1-shared"):
    return upsert_service(
        db, namespace=ns, name=name, node_kind=NODE_KIND_WORKLOAD,
        k8s_uid=uid,
    )


def test_new_node_starts_at_first_incarnation(db):
    svc = _upsert(db, uid="uid-a")

    assert svc.k8s_uid == "uid-a"
    assert svc.incarnation == 1
    assert svc.incarnation_changed_at is None


def test_same_uid_is_not_a_reincarnation(db):
    """Синк ходит каждые 15 минут — счётчик не должен расти на каждом тике."""
    _upsert(db, uid="uid-a")
    svc = _upsert(db, uid="uid-a")

    assert svc.incarnation == 1
    assert svc.incarnation_changed_at is None


def test_changed_uid_bumps_incarnation(db):
    """Тот же namespace и имя, другой объект — значит другое воплощение."""
    _upsert(db, uid="uid-a")
    svc = _upsert(db, uid="uid-b")

    assert svc.k8s_uid == "uid-b"
    assert svc.incarnation == 2
    assert svc.incarnation_changed_at is not None


def test_first_uid_on_existing_node_is_not_a_reincarnation(db):
    """Узел завели алертом (без uid), синк дозаполнил — это не пересоздание.

    Иначе каждый узел, до которого впервые дошёл синк топологии, отчитался бы
    о пересоздании, которого не было.
    """
    _upsert(db, uid=None)
    svc = _upsert(db, uid="uid-a")

    assert svc.k8s_uid == "uid-a"
    assert svc.incarnation == 1
    assert svc.incarnation_changed_at is None


def test_source_without_uid_does_not_erase_known_one(db):
    """Алерт приходит без uid и не должен стирать то, что видел синк."""
    _upsert(db, uid="uid-a")
    svc = _upsert(db, uid=None)

    assert svc.k8s_uid == "uid-a"
    assert svc.incarnation == 1


def test_incarnation_is_per_node_not_per_name(db):
    """Одноимённые узлы в разных namespace живут своей жизнью."""
    _upsert(db, uid="uid-a", ns="squad-1-shared")
    _upsert(db, uid="uid-x", ns="squad-2-shared")
    _upsert(db, uid="uid-b", ns="squad-1-shared")

    by_ns = {s.namespace: s for s in db.query(Service)}
    assert by_ns["squad-1-shared"].incarnation == 2
    assert by_ns["squad-2-shared"].incarnation == 1


def test_target_ref_carries_uid_of_the_workload(db):
    """Ремедиация целится в объект, а не в имя.

    Между планированием действия и его исполнением объект может смениться;
    без uid verify после ремедиации не отличит «под поднялся» от «это уже
    другой Deployment».
    """
    from app.remediation.target_resolver import resolve_target

    _upsert(db, uid="uid-a")
    db.commit()

    ref = resolve_target(
        {"labels": {"namespace": "squad-1-shared", "deployment": "town-service"}},
        kg_session=db,
    )

    assert ref.uid == "uid-a"
    assert ref.incarnation == 1
    assert "kg_workload_uid" in ref.resolved_via
    assert ref.to_dict()["uid"] == "uid-a"


def test_target_ref_without_topology_data_says_so(db):
    """Синк до узла не дошёл — uid нет, и это видно, а не подменяется именем."""
    from app.remediation.target_resolver import resolve_target

    _upsert(db, uid=None)
    db.commit()

    ref = resolve_target(
        {"labels": {"namespace": "squad-1-shared", "deployment": "town-service"}},
        kg_session=db,
    )

    assert ref.uid is None
    assert "kg_workload_uid" not in ref.resolved_via
