"""Blast radius v2: кто пострадает, с эпистемикой на каждом пути и Known Unknowns."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.blast_radius import blast_radius_v2
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)
from app.services.discord.embed_builder import _build_blast_radius_field

NS = "prod-kingdom1"
FRESH = datetime.utcnow() - timedelta(minutes=5)
OLD = datetime.utcnow() - timedelta(days=20)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _node(db, nid, name, kind=NODE_KIND_SERVICE, ns=NS):
    db.add(Service(id=nid, namespace=ns, name=name, node_kind=kind))


def _edge(db, src, dst, kind, sources, last_seen=FRESH, extras=None, discovered_by=None):
    ex = {"discovery_sources": sources, **(extras or {})}
    db.add(ServiceEdge(src_id=src, dst_id=dst, kind=kind, last_seen_at=last_seen, extras=ex,
                       discovered_by=discovered_by or sources[0]))


def _graph(db):
    """town(1/2) ← calls ← auth(3, env → inferred) ← calls ← web(4, runtime → observed)
       town ← calls ← chat(5, inactive)
       entry(6) serves_traffic → town-workload (declared)
       front(7) calls → entry(6)   (2-й шаг через фасад)"""
    _node(db, 1, "town")
    _node(db, 2, "town", NODE_KIND_WORKLOAD)
    _node(db, 3, "auth")
    _node(db, 4, "web")
    _node(db, 5, "chat")
    _node(db, 6, "town-entry")
    _node(db, 7, "front")
    _edge(db, 3, 1, "calls", ["kg_sync/env_vars"])
    _edge(db, 4, 3, "calls", ["kg_sync/runtime_seen"])
    _edge(db, 5, 1, "calls", ["kg_sync/env_vars"], extras={"inactive": True})
    _edge(db, 6, 2, "serves_traffic", ["k8s_topology_resources/service"])
    _edge(db, 7, 6, "calls", ["kg_sync/ingress"])
    db.commit()


def _by_name(out):
    return {e["service"]: e for e in out["impact"]}


def test_impact_walks_two_hops_against_dependencies(db):
    _graph(db)
    out = blast_radius_v2(db, NS, "town")
    imp = _by_name(out)
    assert set(imp) == {"auth", "web", "town-entry", "front"}
    assert imp["auth"]["hops"] == 1 and imp["auth"]["via"] == "calls"
    assert imp["web"]["hops"] == 2 and imp["web"]["path"] == ["town", "auth", "web"]
    assert imp["town-entry"]["via"] == "serves_traffic" and imp["front"]["hops"] == 2
    assert out["summary"]["impacted_total"] == 4
    assert out["summary"]["by_hops"] == {"1": 2, "2": 2}


def test_path_inherits_its_weakest_link(db):
    _graph(db)
    imp = _by_name(blast_radius_v2(db, NS, "town"))
    assert imp["auth"]["epistemic"] == "inferred"                  # env-переменная
    # web → auth наблюдалось (runtime), но auth → town — догадка: путь остаётся догадкой.
    assert imp["web"]["edge_epistemic"] == "observed"
    assert imp["web"]["epistemic"] == "inferred"
    assert imp["town-entry"]["epistemic"] == "declared"
    assert imp["front"]["epistemic"] == "declared"                 # ingress-declared через declared


def test_inactive_edges_are_skipped_and_named(db):
    _graph(db)
    out = blast_radius_v2(db, NS, "town")
    assert "chat" not in _by_name(out)
    assert any(u["scope"] == "inactive_edges" and "1 рёбер" in u["reason"] for u in out["unknowns"])
    with_inactive = blast_radius_v2(db, NS, "town", include_inactive=True)
    assert "chat" in _by_name(with_inactive)


def test_callers_without_runtime_observation_are_a_known_unknown(db):
    _graph(db)
    out = blast_radius_v2(db, NS, "town")
    callers = [u for u in out["unknowns"] if u["scope"] == "callers"]
    assert len(callers) == 1 and "runtime-наблюдений" in callers[0]["reason"]


def test_no_calls_edges_means_callers_unknown_not_absent(db):
    _node(db, 1, "lonely")
    _node(db, 2, "lonely", NODE_KIND_WORKLOAD)
    db.commit()
    out = blast_radius_v2(db, NS, "lonely")
    assert out["impact"] == []
    assert out["unknowns"][0]["scope"] == "callers"
    assert "неизвестны, а не отсутствуют" in out["unknowns"][0]["reason"]


def test_contradicted_edge_marks_the_whole_path(db):
    _node(db, 1, "town")
    _node(db, 2, "town", NODE_KIND_WORKLOAD)
    _node(db, 6, "entry")
    _node(db, 7, "front")
    _edge(db, 6, 2, "serves_traffic", ["k8s_topology_resources/service"], extras={"endpoints_ready": 0})
    _edge(db, 7, 6, "calls", ["kg_sync/runtime_seen"])
    db.commit()
    out = blast_radius_v2(db, NS, "town")
    imp = _by_name(out)
    assert imp["entry"]["epistemic"] == "contradicted" and imp["entry"]["conflicts"]
    assert imp["front"]["epistemic"] == "contradicted"        # наблюдённое ребро не спасает путь
    assert out["summary"]["worst_epistemic"] == "contradicted"
    assert any(u["scope"] == "contradicted" for u in out["unknowns"])


def test_stale_edge_is_stale(db):
    _node(db, 1, "town")
    _node(db, 2, "town", NODE_KIND_WORKLOAD)
    _node(db, 3, "auth")
    _edge(db, 3, 1, "calls", ["kg_sync/runtime_seen"], last_seen=OLD)
    db.commit()
    assert _by_name(blast_radius_v2(db, NS, "town"))["auth"]["epistemic"] == "stale"


def test_database_node_as_target_lists_its_users(db):
    _node(db, 10, "db:postgres:config", kind="db", ns="prod-shared")
    for i, name in enumerate(("auth", "town", "map"), start=11):
        _node(db, i, name)
        _edge(db, i, 10, "uses_db", ["kg_sync/secret_hint"])
    db.commit()
    out = blast_radius_v2(db, "prod-shared", "db:postgres:config")
    assert out["target"]["node_kind"] == "db"
    assert set(_by_name(out)) == {"auth", "town", "map"}
    assert out["summary"]["by_via"] == {"uses_db": 3}
    assert all(e["epistemic"] == "inferred" for e in out["impact"])


def test_hop_limit_reports_what_lies_beyond(db):
    _graph(db)
    out = blast_radius_v2(db, NS, "town", max_hops=1)
    assert set(_by_name(out)) == {"auth", "town-entry"}
    hops = [u for u in out["unknowns"] if u["scope"] == "hops"]
    assert hops and "2 зависимых" in hops[0]["reason"]


def test_impact_sorted_strongest_first_then_nearest(db):
    _graph(db)
    order = [e["service"] for e in blast_radius_v2(db, NS, "town")["impact"]]
    assert order[:2] == ["town-entry", "front"]       # declared (0.8) раньше inferred (0.5)
    assert order[2:] == ["auth", "web"]               # inferred: ближний раньше


def test_legacy_keys_are_preserved_for_the_embed(db):
    _graph(db)
    out = blast_radius_v2(db, NS, "town")
    for key in ("services", "urls", "services_total", "urls_total",
                "services_detailed", "urls_detailed", "min_confidence_seen"):
        assert key in out
    assert out["services"] == ["town-entry"]


def test_unknown_target_keeps_shape(db):
    out = blast_radius_v2(db, NS, "ghost")
    assert out["target"] is None and out["impact"] == [] and out["services"] == []
    assert out["unknowns"][0]["scope"] == "target"


def test_embed_field_shows_impact_legend_and_unknowns(db):
    _graph(db)
    field = _build_blast_radius_field(blast_radius_v2(db, NS, "town"))
    assert field is not None
    text = field["value"]
    assert "4 зависимых (◇2 ≈2)" in text
    assert "◇ `town-entry` ← serves_traffic" in text
    assert "◇ `front` ← calls ·2h" in text          # второй шаг помечен
    assert "≈ `auth` ← calls" in text
    assert "… ещё 1" in text                        # web — четвёртый, за top-3
    assert "❔" in text and "runtime-наблюдений" in text


def test_embed_field_renders_unknowns_even_without_entrypoints(db):
    _node(db, 1, "lonely")
    _node(db, 2, "lonely", NODE_KIND_WORKLOAD)
    db.commit()
    field = _build_blast_radius_field(blast_radius_v2(db, NS, "lonely"))
    assert field is not None and "вызывающие" in field["value"]


def test_embed_field_still_skips_truly_empty_legacy_payload():
    assert _build_blast_radius_field({"services": [], "urls": [], "services_total": 0, "urls_total": 0}) is None
