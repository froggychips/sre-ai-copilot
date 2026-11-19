"""Снимок узлов и рёбер (`populator.UpsertSnapshot`) не меняет результат upsert'ов.

До 18.11.2026 каждый upsert узла или ребра был парой запросов — INSERT …
ON CONFLICT и SELECT строки обратно: 34 млн SELECT kg_services и по 25 млн
INSERT и SELECT kg_service_edges за 18 дней. Снимок грузит узлы и рёбра
namespace пачкой и пропускает upsert, которому нечего менять.

Что держат тесты:

* SQLite: проход со снимком оставляет в базе то же, что проход без него;
  снимок не возвращает удалённое, сбрасывается и выключается переменной;
* PostgreSQL (`requires_postgres`): решение «менять нечего» в Python совпадает
  с условием `where=` в ON CONFLICT — на матрице состояний строки и
  аргументов; повторный проход по namespace не делает ни одного INSERT.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import populator
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)
from tests.conftest import requires_postgres

NS = "snap-kingdom1"
SHARED = "snap-shared"


def _sqlite_session() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False,
                        expire_on_commit=False)()


@pytest.fixture
def db() -> Any:
    session = _sqlite_session()
    try:
        yield session
    finally:
        session.close()


def _topology_pass(db: Any, snapshot: Optional[populator.UpsertSnapshot],
                   owner: str = "team-a", uid_suffix: str = "1",
                   services: int = 12) -> None:
    """Проход синка в миниатюре: узлы, workload-двойники, рёбра трёх видов."""
    kw: Dict[str, Any] = {"snapshot": snapshot} if snapshot is not None else {}
    nats = populator.upsert_service(
        db, SHARED, "nats-shared", team_owner="platform",
        owner_source="platform_static", **kw)
    for i in range(services):
        svc = populator.upsert_service(
            db, NS, f"svc-{i}", metadata={"k8s_service": {"port": 80 + i}},
            owner_fallback=owner, owner_fallback_source="namespace_prefix",
            k8s_uid=f"svc-{i}-{uid_suffix}", **kw)
        workload = populator.upsert_service(
            db, NS, f"svc-{i}", node_kind=NODE_KIND_WORKLOAD,
            team_owner=owner, owner_source="namespace_prefix",
            owner_respect_trust=True, metadata={"k8s_workload": {"replicas": i}},
            k8s_uid=f"wl-{i}-{uid_suffix}", **kw)
        populator.upsert_edge(
            db, src=svc, dst=workload, kind="serves_traffic",
            discovered_by="topology", extras={"selector": {"app": f"svc-{i}"}}, **kw)
        populator.upsert_edge(
            db, src=workload, dst=nats, kind="uses_nats", direction="pub",
            discovered_by="env", **kw)
        populator.upsert_edge(
            db, src=workload, dst=nats, kind="uses_nats", direction="sub",
            discovered_by="env", weight=1 + i % 3, **kw)
        if i:
            prev = populator.upsert_service(
                db, NS, f"svc-{i - 1}", team_owner=owner,
                owner_source="namespace_prefix", **kw)
            populator.upsert_edge(
                db, src=svc, dst=prev, kind="calls",
                discovered_by="env", extras={"confidence": "inferred_env"}, **kw)
    db.commit()


def _state(db: Any) -> Tuple[List[Any], List[Any]]:
    """Содержимое графа без меток времени и суррогатных id."""
    db.expire_all()
    names = {s.id: (s.namespace, s.name, s.node_kind) for s in db.query(Service)}
    services = sorted(
        (s.namespace, s.name, s.node_kind, s.team_owner, s.owner_source,
         s.metadata_json, s.synthetic, s.stale_class, s.k8s_uid, s.incarnation,
         s.incarnation_changed_at is not None)
        for s in db.query(Service)
    )
    edges = sorted(
        (names[e.src_id], names[e.dst_id], e.kind, e.direction, e.weight,
         e.discovered_by, e.extras)
        for e in db.query(ServiceEdge)
    )
    return services, edges


def test_passes_with_snapshot_leave_the_same_graph() -> None:
    plain, snap = _sqlite_session(), _sqlite_session()
    try:
        # Три прохода: создание, повтор без изменений, смена владельца и uid
        # (реинкарнация). Снимок — новый на каждый проход, как в синках.
        for owner, uid in (("team-a", "1"), ("team-a", "1"), ("team-b", "2")):
            _topology_pass(plain, None, owner=owner, uid_suffix=uid)
            _topology_pass(snap, populator.UpsertSnapshot(snap), owner=owner,
                           uid_suffix=uid)
            assert _state(snap) == _state(plain)
        services, edges = _state(snap)
        assert any(row[9] == 2 for row in services), "реинкарнация должна сработать"
        assert len(edges) == 12 * 3 + 11
    finally:
        plain.close()
        snap.close()


def test_snapshot_serves_repeat_pass_without_per_row_selects(db: Any) -> None:
    _topology_pass(db, None)
    statements: List[str] = []

    def _before(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _before)
    try:
        _topology_pass(db, populator.UpsertSnapshot(db))
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # Два namespace узлов и рёбра из одного — три SELECT вместо ~80.
    assert len(selects) == 3, selects


def test_snapshot_does_not_return_deleted_rows(db: Any) -> None:
    _topology_pass(db, None, services=2)
    snapshot = populator.UpsertSnapshot(db)
    svc = snapshot.service(NS, "svc-0", NODE_KIND_SERVICE)
    assert svc is not None
    edges = snapshot.edges_from(svc, "serves_traffic")
    assert len(edges) == 1
    edge = edges[0]

    db.delete(edge)                       # ещё не flush — уже не отдаём
    assert snapshot.edges_from(svc, "serves_traffic") == []
    assert snapshot.edge(svc, edge.dst_id, "serves_traffic", "") is None
    db.flush()
    assert snapshot.edge(svc, edge.dst_id, "serves_traffic", "") is None

    # Upsert после удаления заводит ребро заново, а не возвращает удалённое.
    workload = snapshot.service(NS, "svc-0", NODE_KIND_WORKLOAD)
    again = populator.upsert_edge(db, src=svc, dst=workload, kind="serves_traffic",
                                  snapshot=snapshot)
    db.commit()
    assert again.id is not None
    assert db.query(ServiceEdge).filter_by(
        src_id=svc.id, kind="serves_traffic").count() == 1

    # Удаление мимо ORM снимку надо сообщить явно.
    db.query(ServiceEdge).filter_by(id=again.id).delete(synchronize_session=False)
    snapshot.forget_edges(svc.id, workload.id, "serves_traffic")
    assert snapshot.edge(svc, workload.id, "serves_traffic", "") is None


def test_snapshot_keeps_only_recent_namespaces(db: Any) -> None:
    for i in range(4):
        populator.upsert_service(db, f"snap-ns-{i}", "api")
    db.commit()
    snapshot = populator.UpsertSnapshot(db, max_namespaces=2)
    for i in range(4):
        assert snapshot.service(f"snap-ns-{i}", "api", NODE_KIND_SERVICE) is not None
    assert list(snapshot._services) == ["snap-ns-2", "snap-ns-3"]
    snapshot.clear()
    assert not snapshot._services and not snapshot._edges


def test_snapshot_can_be_switched_off(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert populator.make_upsert_snapshot(db) is not None
    monkeypatch.setenv("KG_UPSERT_SNAPSHOT", "0")
    assert populator.make_upsert_snapshot(db) is None


# ── PostgreSQL: Python-решение «менять нечего» против `where=` в SQL ─────────


@pytest.fixture
def pg_session() -> Any:
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctid(db: Any, table: str, row_id: int) -> Any:
    return db.execute(text(f"SELECT ctid FROM {table} WHERE id = :i"), {"i": row_id}).scalar()


#: Состояния существующей строки: (team_owner, owner_source, synthetic,
#: stale_class, k8s_uid, возраст updated_at в минутах).
_ROW_STATES = [
    (None, None, False, None, None, 1),
    ("", "namespace_prefix", False, None, "uid-1", 1),       # провенанс без владельца
    ("team-a", None, False, "active", "uid-1", 1),
    ("team-a", "namespace_prefix", False, "active", "uid-1", 1),
    ("team-a", "k8s_labels", True, "expected_stale", "uid-1", 1),
    ("team-a", "manual", False, None, None, 1),
    ("team-a", "namespace_prefix", False, "active", "uid-1", 45),  # пора освежить
]

#: Аргументы upsert'а — по ветке set_clause на каждую и их сочетания.
_UPSERT_ARGS: List[Dict[str, Any]] = [
    {},
    {"team_owner": "team-a", "owner_source": "namespace_prefix"},
    {"team_owner": "team-b", "owner_source": "k8s_labels"},
    {"team_owner": "team-a", "owner_source": "namespace_prefix",
     "owner_respect_trust": True},
    {"team_owner": "team-b", "owner_source": "k8s_labels",
     "owner_respect_trust": True},
    {"team_owner": "team-a", "owner_source": None, "owner_respect_trust": True},
    {"owner_fallback": "team-f", "owner_fallback_source": "namespace_prefix"},
    {"owner_fallback": "team-f"},
    {"stale_class": "active"},
    {"synthetic": True},
    {"synthetic": False, "k8s_uid": "uid-1"},
    {"k8s_uid": "uid-2"},
    {"team_owner": "team-a", "owner_source": "namespace_prefix",
     "stale_class": "active", "k8s_uid": "uid-1", "synthetic": False},
]


def _seed_row(db: Any, name: str, state: Tuple[Any, ...]) -> int:
    owner, source, synthetic, stale_class, uid, age_min = state
    svc = Service(namespace=NS, name=name, team_owner=owner, owner_source=source,
                  synthetic=synthetic, stale_class=stale_class, k8s_uid=uid,
                  incarnation=1)
    db.add(svc)
    db.flush()
    db.execute(
        text("UPDATE kg_services SET updated_at = :t WHERE id = :i"),
        {"t": datetime.utcnow() - timedelta(minutes=age_min), "i": svc.id},
    )
    return int(svc.id)


def _row(db: Any, row_id: int) -> Tuple[Any, ...]:
    return tuple(db.execute(text(
        "SELECT team_owner, owner_source, synthetic, stale_class, k8s_uid, "
        "incarnation, incarnation_changed_at IS NOT NULL, metadata_json::text "
        "FROM kg_services WHERE id = :i"), {"i": row_id}).one())


@requires_postgres
def test_service_noop_decision_mirrors_sql_where(pg_session: Any) -> None:
    """Снимок пропускает upsert тогда и только тогда, когда SQL не пишет строку."""
    db = pg_session
    db.query(ServiceEdge).filter(ServiceEdge.src_id.in_(
        db.query(Service.id).filter(Service.namespace == NS))).delete(
            synchronize_session=False)
    db.query(Service).filter(Service.namespace == NS).delete(synchronize_session=False)
    cases = list(itertools.product(range(len(_ROW_STATES)), range(len(_UPSERT_ARGS))))
    ids: Dict[Tuple[int, int, str], int] = {}
    for si, ai in cases:
        for side in ("plain", "snap"):
            ids[(si, ai, side)] = _seed_row(db, f"n-{si}-{ai}-{side}", _ROW_STATES[si])
    db.expire_all()

    skipped = written = 0
    for si, ai in cases:
        kwargs = _UPSERT_ARGS[ai]
        plain_id, snap_id = ids[(si, ai, "plain")], ids[(si, ai, "snap")]

        ctid_before = _ctid(db, "kg_services", plain_id)
        populator.upsert_service(db, NS, f"n-{si}-{ai}-plain", **kwargs)
        sql_wrote = _ctid(db, "kg_services", plain_id) != ctid_before

        snapshot = populator.UpsertSnapshot(db)
        snap_ctid = _ctid(db, "kg_services", snap_id)
        populator.upsert_service(db, NS, f"n-{si}-{ai}-snap", snapshot=snapshot,
                                 **kwargs)
        snapshot.flush()                  # отложенное касание метки
        snap_wrote = _ctid(db, "kg_services", snap_id) != snap_ctid

        label = f"строка {_ROW_STATES[si]} аргументы {kwargs}"
        assert snap_wrote == sql_wrote, label
        assert _row(db, snap_id) == _row(db, plain_id), label
        skipped += not sql_wrote
        written += sql_wrote
    # Матрица должна покрывать обе стороны, иначе зеркало не проверено.
    assert skipped > 20 and written > 20, (skipped, written)


@requires_postgres
def test_edge_noop_decision_mirrors_sql_where(pg_session: Any) -> None:
    db = pg_session
    db.query(ServiceEdge).filter(ServiceEdge.src_id.in_(
        db.query(Service.id).filter(Service.namespace == NS))).delete(
            synchronize_session=False)
    db.query(Service).filter(Service.namespace == NS).delete(synchronize_session=False)
    src = populator.upsert_service(db, NS, "edge-src")
    states = [(age_h, weight) for age_h in (0, 2, 4, None) for weight in (1, 3, None)]
    incoming = (1, 3, 5)
    checked = 0
    for (age_h, weight), new_weight in itertools.product(states, incoming):
        rows = {}
        for side in ("plain", "snap"):
            dst = populator.upsert_service(db, NS, f"dst-{age_h}-{weight}-{new_weight}-{side}")
            edge = ServiceEdge(
                src_id=src.id, dst_id=dst.id, kind="calls", direction="",
                weight=weight, discovered_by="s0",
                extras={"discovery_sources": ["s0"]},
                last_seen_at=(None if age_h is None
                              else datetime.utcnow() - timedelta(hours=age_h)),
            )
            db.add(edge)
            db.flush()
            rows[side] = (dst, int(edge.id))
        db.expire_all()

        dst, edge_id = rows["plain"]
        before = _ctid(db, "kg_service_edges", edge_id)
        populator.upsert_edge(db, src=src, dst=dst, kind="calls",
                              weight=new_weight, discovered_by="s1")
        sql_wrote = _ctid(db, "kg_service_edges", edge_id) != before
        plain = _edge_row(db, edge_id)

        dst, edge_id = rows["snap"]
        snapshot = populator.UpsertSnapshot(db)
        before = _ctid(db, "kg_service_edges", edge_id)
        returned = populator.upsert_edge(db, src=src, dst=dst, kind="calls",
                                         weight=new_weight, discovered_by="s1",
                                         snapshot=snapshot)
        snapshot.flush()                  # отложенное касание метки
        snap_wrote = _ctid(db, "kg_service_edges", edge_id) != before
        snap = _edge_row(db, edge_id)

        label = f"age_h={age_h} weight={weight} new={new_weight}"
        assert snap_wrote == sql_wrote, label
        # weight, discovered_by (на касании его переписывает тот, кто видел),
        # extras и свежесть метки — как после обычного upsert'а.
        assert snap == plain, label
        # Объект, который вернул upsert, не расходится с базой.
        assert (returned.weight, returned.discovered_by) == snap[:2], label
        checked += 1
    assert checked == len(states) * len(incoming)


def _edge_row(db: Any, edge_id: int) -> Tuple[Any, ...]:
    return tuple(db.execute(text(
        "SELECT weight, discovered_by, extras::text, "
        "last_seen_at >= (now() at time zone 'utc') - interval '1 minute' "
        "FROM kg_service_edges WHERE id = :i"), {"i": edge_id}).one())


@requires_postgres
def test_stale_marks_are_touched_in_one_update_per_batch(pg_session: Any) -> None:
    """Проход, где всем строкам пора освежить метку: UPDATE на пачку, не на строку."""
    db = pg_session
    for ns in (NS, SHARED):
        db.query(ServiceEdge).filter(ServiceEdge.src_id.in_(
            db.query(Service.id).filter(Service.namespace == ns))).delete(
                synchronize_session=False)
        db.query(Service).filter(Service.namespace == ns).delete(
            synchronize_session=False)
    _topology_pass_no_commit(db, None)
    db.flush()
    ids = [i for (i,) in db.query(Service.id).filter(Service.namespace.in_([NS, SHARED]))]
    db.execute(text("UPDATE kg_services SET updated_at = updated_at - interval '2 hours' "
                    "WHERE id = ANY(:ids)"), {"ids": ids})
    db.execute(text("UPDATE kg_service_edges SET last_seen_at = last_seen_at - interval '5 hours' "
                    "WHERE src_id = ANY(:ids)"), {"ids": ids})
    db.expire_all()

    statements: List[str] = []

    def _before(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        statements.append(" ".join(statement.split())[:40])

    snapshot = populator.UpsertSnapshot(db)
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _before)
    try:
        _topology_pass_no_commit(db, snapshot)
        snapshot.flush()
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    assert not any(s.startswith("INSERT") for s in statements), statements
    updates = [s for s in statements if s.startswith("UPDATE")]
    # Один UPDATE узлов и по одному на источник рёбер (topology, env).
    assert len(updates) == 3, updates
    stale_nodes = db.execute(text(
        "SELECT count(*) FROM kg_services WHERE id = ANY(:ids) "
        "AND updated_at < (now() at time zone 'utc') - interval '30 minutes'"),
        {"ids": ids}).scalar()
    stale_edges = db.execute(text(
        "SELECT count(*) FROM kg_service_edges WHERE src_id = ANY(:ids) "
        "AND last_seen_at < (now() at time zone 'utc') - interval '3 hours'"),
        {"ids": ids}).scalar()
    assert stale_nodes == 0 and stale_edges == 0


@requires_postgres
def test_repeat_pass_on_postgres_runs_no_upserts(pg_session: Any) -> None:
    """Повторный проход по namespace: только SELECT-ы загрузки снимка."""
    db = pg_session
    for ns in (NS, SHARED):
        db.query(ServiceEdge).filter(ServiceEdge.src_id.in_(
            db.query(Service.id).filter(Service.namespace == ns))).delete(
                synchronize_session=False)
        db.query(Service).filter(Service.namespace == ns).delete(
            synchronize_session=False)

    def _run(snapshot: Optional[populator.UpsertSnapshot]) -> List[str]:
        statements: List[str] = []

        def _before(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
            statements.append(" ".join(statement.split())[:60])

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _before)
        try:
            # Без commit: фикстура откатывает транзакцию в конце теста.
            _topology_pass_no_commit(db, snapshot)
        finally:
            event.remove(engine, "before_cursor_execute", _before)
        return statements

    _run(None)                                   # создание
    plain = _run(None)                           # повтор по-старому
    snap = _run(populator.UpsertSnapshot(db))    # повтор со снимком
    assert _state_pg(db) is not None

    assert sum(s.startswith("INSERT") for s in plain) > 80
    assert not any(s.startswith(("INSERT", "UPDATE")) for s in snap), snap
    assert len(snap) == 3, snap                  # узлы двух namespace и рёбра
    assert len(plain) / len(snap) > 50


def _topology_pass_no_commit(db: Any, snapshot: Optional[populator.UpsertSnapshot]) -> None:
    commit = db.commit
    db.commit = lambda: None  # type: ignore[method-assign]
    try:
        _topology_pass(db, snapshot)
    finally:
        db.commit = commit  # type: ignore[method-assign]


def _state_pg(db: Any) -> Any:
    return db.query(Service).filter(Service.namespace == NS).count()
