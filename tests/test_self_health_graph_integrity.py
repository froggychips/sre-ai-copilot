"""Тесты check_graph_integrity — regression-watch инвариантов графа (#185/#189/#190)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import Namespace, Service, ServiceEdge
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


def _ns(db, name, state="active"):
    n = Namespace(namespace=name, state=state)
    db.add(n)
    db.flush()
    return n


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
    assert r.detail["db_dup_names_within_ns"] == 0
    assert r.detail["self_loops_any"] == 0
    assert r.detail["dangling_edges"] == 0


def test_same_db_name_in_different_namespaces_is_normal(db):
    """Одно имя БД в разных окружениях — не дубль, а разные физические базы.

    Регрессия на ложный fail, из-за которого проверка звучала постоянно.
    У каждого сквада своё окружение с полным набором БД: замер на проде
    20.08.2026 показал `db:postgres:message` в 56 namespace, причём каждый
    узел собирал рёбра только своего окружения — узел в `squad-1-shared`
    обслуживал `squad-1-*`, узел в `prod-shared` — `prod-*`. Глобальная
    уникальность имени БД в такой инфраструктуре не инвариант.
    """
    _svc(db, "db:postgres:town", "squad-1-shared")
    _svc(db, "db:postgres:town", "squad-2-shared")
    _svc(db, "db:postgres:town", "prod-shared")
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "ok", "одноимённые БД разных окружений снова читаются как дубли"
    assert r.detail["db_dup_names_within_ns"] == 0


def test_duplicate_in_one_namespace_is_prevented_by_the_schema(db):
    """Дубль внутри namespace невозможен: его держит констрейнт, не проверка.

    Попытка создать второй узел с тем же (namespace, name, node_kind) падает
    на `uq_kg_service_ns_name_kind` ещё до всякой самопроверки. Счётчик
    `db_dup_names_within_ns` в graph_integrity поэтому и остаётся — он
    сторожит не синк, а сам констрейнт: >0 будет означать, что констрейнт
    сняли миграцией.
    """
    from sqlalchemy.exc import IntegrityError

    _svc(db, "db:postgres:town", "squad-1-shared")
    with pytest.raises(IntegrityError):
        _svc(db, "db:postgres:town", "squad-1-shared")
    db.rollback()


def test_live_service_pointing_at_db_in_missing_namespace(db):
    """Ребро из живого namespace в базу удалённого — неверный факт о проде.

    Так выглядит незавершённая консолидация db-узлов. `phantom_db_cleanup`
    схлопывал копии в узел с лексикографически МИНИМАЛЬНЫМ namespace, и
    `preprod-kingdom1` оказался минимумом среди всех окружений; фикс 15.08
    (`shared_namespace_of`) исправил только новые рёбра. На 20.08.2026 в
    графе было 3614 таких рёбер — и прежняя проверка их не видела.
    """
    _ns(db, "prod-shared", "active")
    _ns(db, "preprod-kingdom1", "missing")
    dead_db = _svc(db, "db:postgres:town", "preprod-kingdom1")
    # 150 РАЗНЫХ сервисов: (src,dst,kind) уникален по констрейнту рёбер, и
    # на проде эти 3614 — тоже 3614 разных источников, а не повторы одного.
    for i in range(150):   # выше порога _GRAPH_INTEGRITY_FAIL_STALE_DB_EDGES
        live = _svc(db, f"town-service-{i}", "prod-shared")
        _edge(db, live.id, dead_db.id, kind="uses_db")
    db.commit()
    r = check_graph_integrity(db)
    assert r.status == "fail"
    assert r.detail["live_edges_into_missing_ns_db"] == 150


def test_dead_to_dead_db_edges_are_not_counted(db):
    """Снесённый сквад ссылается на свою же базу — это мусор, а не ложь.

    У удалённого окружения свои сервисы и свои БД, и рёбра между ними
    внутренние (~102 на сквад в замере 20.08.2026). Их убирает retention в
    `namespace_lifecycle`; объявлять их порчей целостности — значит держать
    проверку в fail до конца retention-окна.
    """
    _ns(db, "squad-23-shared", "missing")
    dead_db = _svc(db, "db:postgres:town", "squad-23-shared")
    for i in range(150):
        dead_svc = _svc(db, f"town-service-{i}", "squad-23-shared")
        _edge(db, dead_svc.id, dead_db.id, kind="uses_db")
    db.commit()
    r = check_graph_integrity(db)
    assert r.detail["live_edges_into_missing_ns_db"] == 0
    assert r.status == "ok"


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
