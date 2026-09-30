"""Источник kg_nodes в контексте инцидента: зона ноды алерта, соседи по зоне
и host-ы, чей трафик входит через неё (app/context/kg_incident_context.py)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.context import kg_incident_context as kgi
from app.database import Base
from app.knowledge_graph.schema import K8sNode, Service, TrafficEntrypoint
from app.models.incident import Incident

AS_OF = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
N = AS_OF.replace(tzinfo=None)
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _cli_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")


def _n(name, zone, *, first=N - 48 * H, deleted=None, ip="10.0.0.1", unsched=False):
    return K8sNode(name=name, zone=zone, region="r-1", internal_ip=ip,
                   addresses=[{"type": "InternalIP", "address": ip}], roles=[],
                   unschedulable=unsched, labels_json={}, first_seen_at=first,
                   last_seen_at=N, deleted_at=deleted)


def _e(host, nodes, *, first=N - 48 * H, deleted=None, last=N):
    return TrafficEntrypoint(host=host, resolved_ips=["203.0.113.10"], entry_nodes=nodes,
                             lb_services=["ingress/controller"] if nodes else [],
                             ingress_classes=["nginx"], first_seen_at=first,
                             last_seen_at=last, deleted_at=deleted)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    s.add(Service(namespace="app-1", name="web"))
    s.add_all([
        _n("node-a", "dc-1", ip="10.0.0.1", unsched=True),
        _n("node-b", "dc-1", ip="10.0.0.2"),
        # Появилась после as_of — на момент инцидента её не было.
        _n("node-c", "dc-1", first=N + H, ip="10.0.0.3"),
        # Удалена до as_of.
        _n("node-d", "dc-1", deleted=N - H, ip="10.0.0.4"),
        # Удалена после as_of — на момент инцидента жива.
        _n("node-e", "dc-1", deleted=N + H, ip="10.0.0.5"),
        _n("node-f", "dc-2", ip="10.0.0.6"),
        _e("app.example.test", ["node-a"]),
        _e("api.example.test", ["node-a", "node-b"]),
        _e("late.example.test", ["node-a"], first=N + H),
        _e("old.example.test", ["node-a"], deleted=N - H),
        _e("other.example.test", ["node-f"]),
    ])
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _kgc(db, **over):
    base = dict(namespace="app-1", service="web", alertname="NodeDown", as_of=AS_OF)
    base.update(over)
    return kgi.fetch_kg_incident_context(kgi.SessionReader(db), kgi.KGScope(**base))


def test_node_topology_point_in_time(db):
    kgc = _kgc(db, node="node-a")
    nt = kgc["node_topology"]
    assert kgc["sources"]["kg_nodes"]["status"] == "success"
    assert nt["zone"] == "dc-1" and nt["region"] == "r-1" and nt["unschedulable"]
    # Соседи по зоне на as_of: без будущей node-c и удалённой node-d.
    assert nt["same_zone_nodes"] == ["node-b", "node-e"] and nt["same_zone_count"] == 2
    assert nt["entry_hosts"] == ["api.example.test", "app.example.test"]
    assert nt["entry_lb_services"] == ["ingress/controller"]
    assert not nt["observed_after_as_of"]
    json.dumps(kgc)                                   # хранится датасетом как JSON


def test_node_matched_by_address_label(db):
    nt = _kgc(db, node="10.0.0.6:9100")["node_topology"]
    assert nt["node"] == "node-f" and nt["same_zone_nodes"] == []
    assert nt["entry_hosts"] == ["other.example.test"]


def test_unknown_node_is_empty_and_later_mapping_is_flagged(db):
    kgc = _kgc(db, node="node-zzz")
    assert kgc["node_topology"] is None and kgc["sources"]["kg_nodes"]["status"] == "empty"
    assert "[kg_nodes]" not in kgi.kg_context_prompt(kgc)
    db.query(TrafficEntrypoint).filter_by(host="app.example.test").one().last_seen_at = N + 5 * H
    db.commit()
    assert _kgc(db, node="node-a")["node_topology"]["observed_after_as_of"]


def test_node_absent_changes_nothing(db):
    kgc = _kgc(db)
    assert "kg_nodes" not in kgc["sources"]
    assert "node_topology" not in kgc and "node" not in kgc
    ctx = kgi.apply_kg_context({"service": "web", "source_status": {}}, kgc)
    assert "kg_node_topology" not in ctx
    assert "[kg_nodes]" not in (ctx.get("k8s_summary") or "")
    assert "[kg_nodes]" not in kgi.kg_context_prompt(kgc)


def test_apply_and_prompt_render_node_line(db):
    kgc = _kgc(db, node="node-a")
    ctx = kgi.apply_kg_context({"service": "web", "source_status": {}}, kgc)
    assert ctx["kg_node_topology"]["node"] == "node-a"
    assert "[kg_nodes]\nNode node-a: zone dc-1 (region r-1)" in ctx["k8s_summary"]
    text = kgi.kg_context_prompt(kgc)
    line = next(ln for ln in text.splitlines() if ln.startswith("[kg_nodes]"))
    assert "same-zone nodes (2): node-b, node-e" in line
    assert "traffic entry for hosts (2): api.example.test, app.example.test" in line
    assert "unschedulable" in line


def test_failed_node_source_is_unknown_not_absent(db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no table")

    monkeypatch.setattr(kgi, "_SOURCES", tuple(
        (n, boom if n == "kg_nodes" else f) for n, f in kgi._SOURCES))
    kgc = _kgc(db, node="node-a")
    assert kgc["sources"]["kg_nodes"]["status"] == "failed"
    ctx = kgi.apply_kg_context({"service": "web", "source_status": {}}, kgc)
    assert ctx["source_status"]["kg_node_topology"].startswith("kg_nodes недоступен")
    assert "kg_nodes" in kgi.kg_context_prompt(kgc)


def test_all_other_sources_failed_without_node_is_still_failed(db, monkeypatch):
    """Пропущенный (не опрошенный) источник не превращает FAILED в PARTIAL."""
    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(kgi, "_SOURCES", tuple((n, boom) for n, _f in kgi._SOURCES))
    out = kgi.collect_kg_incident_context(
        kgi.SessionReader(db), kgi.KGScope(namespace="app-1", as_of=AS_OF))
    assert out.status.value == "failed"


def test_diagnostics_ctx_passes_node_label(db, monkeypatch):
    from app.diagnostics import incident_ctx

    monkeypatch.setattr(incident_ctx, "nearby_alerts", lambda *_a, **_k: [])
    inc = Incident(incident_id="i1", severity="warning", status="firing", summary="s",
                   namespace="app-1",
                   labels={"alertname": "NodeDown", "namespace": "app-1", "service": "web",
                           "node": "node-a"},
                   annotations={}, starts_at=AS_OF.isoformat())
    ctx = incident_ctx.build_diagnostics_ctx(inc, "", kg_session=db)
    assert ctx["kg_node_topology"]["zone"] == "dc-1"
    assert "[kg_nodes]" in ctx["k8s_summary"]


def test_dataset_case_scope_takes_alert_node():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "live_rca_dataset.py"
    spec = importlib.util.spec_from_file_location("live_rca_dataset_t", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    scope = mod.case_scope({"namespace": "app-1", "started_at": AS_OF.isoformat(),
                            "alert_node": "node-a"})
    assert scope.node == "node-a"
    assert mod.case_scope({"namespace": "app-1", "started_at": AS_OF.isoformat()}).node is None


def test_node_alert_without_namespace_collects_only_nodes(db):
    """Нодовый алерт без стенда (NodeDiskIOSaturation и т.п.): namespace нет,
    а зона ноды и host-ы входа нужны — опрашивается только kg_nodes."""
    out = kgi.build_kg_context(db, namespace=None, service=None, alertname="NodeDown",
                               as_of=AS_OF, node="node-a")
    assert out is not None and out.status.value == "success"
    kgc = out.data
    assert set(kgc["sources"]) == {"kg_nodes"}
    assert kgc["namespaces"] == [] and kgc["ns_scope"] is None
    assert kgc["node_topology"]["zone"] == "dc-1"
    assert "[kg_nodes]" in kgi.kg_context_prompt(kgc)


def test_build_kg_context_needs_namespace_or_node(db):
    assert kgi.build_kg_context(db, namespace=None, service=None, alertname="X",
                                as_of=AS_OF) is None
    assert kgi.build_kg_context(db, namespace="Bad NS!", service=None, alertname="X",
                                as_of=AS_OF, node=" ") is None


def test_diagnostics_ctx_node_alert_without_namespace(db, monkeypatch):
    from app.diagnostics import incident_ctx

    monkeypatch.setattr(incident_ctx, "nearby_alerts", lambda *_a, **_k: [])
    inc = Incident(incident_id="i2", severity="warning", status="firing", summary="s",
                   namespace=None, labels={"alertname": "NodeDown", "node": "node-a"},
                   annotations={}, starts_at=AS_OF.isoformat())
    ctx = incident_ctx.build_diagnostics_ctx(inc, "", kg_session=db)
    assert ctx["kg_node_topology"]["zone"] == "dc-1"
    assert "[kg_nodes]" in ctx["k8s_summary"]
