"""Синки топологии со снимком узлов и рёбер оставляют тот же граф, что без него.

Сквозная проверка трёх синков, которые до 18.11.2026 давали почти все
upsert'ы графа (pg_stat_statements: 25 млн INSERT kg_service_edges, 34 млн
SELECT kg_services за 18 дней): `kg_sync.sync_namespace` + Pass 2,
`k8s_topology_resources_sync.sync_all_services`, `k8s_endpoints_sync`.

Один и тот же «кластер» прогоняется дважды — с `KG_UPSERT_SNAPSHOT=0`
(прежний путь) и со снимком — через три цикла: создание, повтор без
изменений, изменения (новый selector, пересозданный Service, новый сервис).
Граф после каждого цикла должен совпасть. На PostgreSQL дополнительно
считается число запросов повторного цикла — это и есть ожидаемый эффект.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import k8s_endpoints_sync as eps
from app.knowledge_graph import k8s_topology_resources_sync as topo
from app.knowledge_graph import kg_sync as ks
from app.knowledge_graph.schema import Service, ServiceEdge
from tests.conftest import requires_postgres

REALM = "e2esnap"
SHARED = f"{REALM}-shared"
KINGDOMS = [f"{REALM}-kingdom{i}" for i in (1, 2, 3)]
NAMESPACES = [SHARED, *KINGDOMS]


def _deploy(name: str, ns: str, env: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns, "uid": f"dep-{ns}-{name}"},
        "spec": {"template": {
            "metadata": {"labels": {"app": name}},
            "spec": {"containers": [{"env": env}]},
        }},
    }


def _service(name: str, ns: str, selector: Dict[str, str], uid: str = "1") -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns, "uid": f"svc-{ns}-{name}-{uid}"},
        "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.1",
                 "ports": [{"port": 80, "targetPort": 8080}], "selector": selector},
    }


def _cluster(per_ns: int = 10, variant: int = 0) -> Dict[str, Any]:
    """Кластер-фикстура. variant=1 — тот же кластер после изменений."""
    deploys: Dict[str, List[Dict[str, Any]]] = {}
    services: List[Dict[str, Any]] = []
    endpoints: List[Dict[str, Any]] = []
    for ns in NAMESPACES:
        names = [f"svc{i}-service" for i in range(per_ns)]
        if variant and ns == KINGDOMS[0]:
            names.append("late-service")              # новый сервис
        deploys[ns] = []
        for i, name in enumerate(names):
            env: List[Dict[str, Any]] = [
                {"name": "AUTH_URL",
                 "value": f"http://svc0-service.{SHARED}.svc.cluster.local:8080"},
                {"name": "SHARED_NATS_CONNECTION", "value": ""},
            ]
            if ns != SHARED:
                env.append({"name": "KINGDOM_NATS_CONNECTION", "value": ""})
                env.append({"name": "PEER_HOST",
                            "value": f"svc{(i + 1) % per_ns}-service.{ns}"})
            deploys[ns].append(_deploy(name, ns, env))
            selector = {"app": name}
            uid = "1"
            if variant and ns == KINGDOMS[1] and i == 0:
                # Service переключили на другой backend.
                selector = {"app": names[1]}
            if variant and ns == KINGDOMS[1] and i == 2:
                uid = "2"                              # Service пересоздали
            services.append(_service(name, ns, selector, uid))
            ready = 0 if (i == 3 and ns == KINGDOMS[2]) else 1 + (i + variant) % 2
            endpoints.append({
                "metadata": {"namespace": ns, "name": name},
                "subsets": ([{"addresses": [{"ip": f"10.0.0.{k}"} for k in range(ready)]}]
                            if ready else []),
            })
    return {"deploys": deploys, "services": services, "endpoints": endpoints}


def _cycle(db: Any, cluster: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Один круг трёх синков в порядке расписания."""
    monkeypatch.setattr(
        topo, "_kubectl_get_all",
        lambda resource: cluster["services"] if resource == "services" else [],
    )
    all_deploys = [d for ds in cluster["deploys"].values() for d in ds]
    topo.sync_all_services(db, topo._index_deployments_by_ns(all_deploys))

    ks._reset_existing_ns_cache()
    for ns, deploys in cluster["deploys"].items():
        with db.begin_nested():
            ks.sync_namespace(db, ns, deploys=deploys)
        db.commit()
    known = ks._build_known_index(db)
    for ns, deploys in cluster["deploys"].items():
        with db.begin_nested():
            ks._enrich_calls_edges_for_ns(db, ns, deploys, known)
        db.commit()

    monkeypatch.setattr(eps, "_fetch_endpoints", lambda: cluster["endpoints"])
    eps.sync_endpoints(db)


def _graph(db: Any) -> Tuple[List[Any], List[Any]]:
    """Граф фикстуры без суррогатных id и меток времени."""
    db.expire_all()
    nodes = db.query(Service).filter(Service.namespace.in_(NAMESPACES)).all()
    names = {s.id: (s.namespace, s.name, s.node_kind) for s in nodes}

    def _meta(meta: Any) -> Any:
        # endpoints_checked_at — метка времени записи, остальное сравнимо.
        if not isinstance(meta, dict):
            return meta
        return {k: v for k, v in meta.items() if k != "endpoints_checked_at"}

    services = sorted(
        (s.namespace, s.name, s.node_kind, s.team_owner, s.owner_source,
         repr(_meta(s.metadata_json)), s.synthetic, s.stale_class, s.k8s_uid,
         s.incarnation, s.incarnation_changed_at is not None)
        for s in nodes
    )
    edges = sorted(
        (names[e.src_id], names[e.dst_id], e.kind, e.direction, e.weight,
         e.discovered_by, repr(e.extras))
        for e in db.query(ServiceEdge).filter(ServiceEdge.src_id.in_(list(names)))
    )
    return services, edges


class _Counter:
    def __init__(self, db: Any) -> None:
        self.engine = db.get_bind()
        self.by_kind: Dict[str, int] = {}

    def _before(self, conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        key = ks._sql_key(statement)
        self.by_kind[key] = self.by_kind.get(key, 0) + 1

    def __enter__(self) -> "_Counter":
        event.listen(self.engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc: Any) -> None:
        event.remove(self.engine, "before_cursor_execute", self._before)

    @property
    def total(self) -> int:
        return sum(self.by_kind.values())


def _run_three_cycles(
    db: Any, monkeypatch: pytest.MonkeyPatch, snapshot_on: bool,
) -> Tuple[List[Any], _Counter]:
    monkeypatch.setenv("KG_UPSERT_SNAPSHOT", "1" if snapshot_on else "0")
    states = []
    _cycle(db, _cluster(), monkeypatch)                 # создание
    states.append(_graph(db))
    with _Counter(db) as steady:
        _cycle(db, _cluster(), monkeypatch)             # повтор без изменений
    states.append(_graph(db))
    _cycle(db, _cluster(variant=1), monkeypatch)        # изменения
    states.append(_graph(db))
    return states, steady


def _sqlite() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False,
                        expire_on_commit=False)()


def test_three_sync_cycles_match_without_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    plain_db, snap_db = _sqlite(), _sqlite()
    try:
        plain, _ = _run_three_cycles(plain_db, monkeypatch, snapshot_on=False)
        snap, _ = _run_three_cycles(snap_db, monkeypatch, snapshot_on=True)
        for i, (a, b) in enumerate(zip(plain, snap)):
            assert b[0] == a[0], f"узлы разошлись в цикле {i}"
            assert b[1] == a[1], f"рёбра разошлись в цикле {i}"
        services, edges = snap[-1]
        assert len(services) > 80 and len(edges) > 100
        # Изменения третьего цикла действительно отразились в графе.
        assert any(row[9] == 2 for row in services), "пересозданный Service"
        assert any(row[1] == "late-service" for row in services)
    finally:
        plain_db.close()
        snap_db.close()


def _wipe(db: Any) -> None:
    ids = [i for (i,) in db.query(Service.id).filter(Service.namespace.in_(NAMESPACES))]
    if ids:
        db.query(ServiceEdge).filter(
            ServiceEdge.src_id.in_(ids) | ServiceEdge.dst_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(Service).filter(Service.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.expunge_all()


def _aged_cycle(db: Any, monkeypatch: pytest.MonkeyPatch, snapshot_on: bool) -> _Counter:
    """Цикл, в котором всем меткам свежести пора обновиться (PostgreSQL)."""
    monkeypatch.setenv("KG_UPSERT_SNAPSHOT", "1" if snapshot_on else "0")
    ids = [i for (i,) in db.query(Service.id).filter(Service.namespace.in_(NAMESPACES))]
    db.execute(text("UPDATE kg_services SET updated_at = updated_at - interval '2 hours' "
                    "WHERE id = ANY(:ids)"), {"ids": ids})
    db.execute(text("UPDATE kg_service_edges SET last_seen_at = last_seen_at - interval "
                    "'5 hours' WHERE src_id = ANY(:ids)"), {"ids": ids})
    db.commit()
    db.expire_all()
    with _Counter(db) as counter:
        _cycle(db, _cluster(variant=1), monkeypatch)
    stale_nodes = db.execute(text(
        "SELECT count(*) FROM kg_services WHERE id = ANY(:ids) "
        "AND updated_at < (now() at time zone 'utc') - interval '30 minutes'"),
        {"ids": ids}).scalar()
    stale_edges = db.execute(text(
        "SELECT count(*) FROM kg_service_edges WHERE src_id = ANY(:ids) "
        "AND last_seen_at < (now() at time zone 'utc') - interval '3 hours'"),
        {"ids": ids}).scalar()
    counter.by_kind["_stale_nodes"] = int(stale_nodes or 0)
    counter.by_kind["_stale_edges"] = int(stale_edges or 0)
    return counter


@requires_postgres
def test_three_sync_cycles_match_on_postgres_and_cut_queries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import SessionLocal, engine

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        _wipe(db)
        plain, plain_steady = _run_three_cycles(db, monkeypatch, snapshot_on=False)
        plain_aged = _aged_cycle(db, monkeypatch, snapshot_on=False)
        plain.append(_graph(db))
        _wipe(db)
        snap, snap_steady = _run_three_cycles(db, monkeypatch, snapshot_on=True)
        snap_aged = _aged_cycle(db, monkeypatch, snapshot_on=True)
        snap.append(_graph(db))
        for i, (a, b) in enumerate(zip(plain, snap)):
            assert b[0] == a[0], f"узлы разошлись в цикле {i}"
            assert b[1] == a[1], f"рёбра разошлись в цикле {i}"

        # Цикл, где всем меткам пора освежиться: метки освежены в обоих
        # режимах одинаково (несвежими остаются одни и те же строки — те,
        # которых синки в этом цикле не видели), но касания идут пачками.
        assert snap_aged.by_kind.pop("_stale_nodes") == plain_aged.by_kind.pop("_stale_nodes")
        assert snap_aged.by_kind.pop("_stale_edges") == plain_aged.by_kind.pop("_stale_edges")
        assert snap_aged.by_kind.get("insert into kg_services", 0) == 0
        assert snap_aged.by_kind.get("insert into kg_service_edges", 0) == 0
        assert plain_aged.total / snap_aged.total > 8

        with capsys.disabled():
            print(f"\nповторный цикл, запросов: без снимка {plain_steady.total}, "
                  f"со снимком {snap_steady.total}")
            for key in sorted(set(plain_steady.by_kind) | set(snap_steady.by_kind)):
                print(f"  {key:34s} {plain_steady.by_kind.get(key, 0):6d} → "
                      f"{snap_steady.by_kind.get(key, 0):6d}")
            print(f"цикл с устаревшими метками, запросов: без снимка {plain_aged.total}, "
                  f"со снимком {snap_aged.total}")
            for key in sorted(set(plain_aged.by_kind) | set(snap_aged.by_kind)):
                print(f"  {key:34s} {plain_aged.by_kind.get(key, 0):6d} → "
                      f"{snap_aged.by_kind.get(key, 0):6d}")

        # Повторный цикл со снимком ничего не пишет в узлы и рёбра …
        assert snap_steady.by_kind.get("insert into kg_services", 0) == 0
        assert snap_steady.by_kind.get("insert into kg_service_edges", 0) == 0
        # … а без снимка это upsert на каждый узел и каждое ребро.
        assert plain_steady.by_kind["insert into kg_services"] > 100
        assert plain_steady.by_kind["insert into kg_service_edges"] > 100
        assert plain_steady.total / snap_steady.total > 8
    finally:
        _wipe(db)
        db.close()
