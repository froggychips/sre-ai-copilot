"""kg_nodes / kg_entrypoints: зоны нод и точки входа трафика
(app/knowledge_graph/k8s_nodes_sync.py). kubectl и DNS замоканы."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import k8s_nodes_sync as m
from app.knowledge_graph.kubectl_breaker import KubectlCircuitOpen
from app.knowledge_graph.schema import K8sNode, TrafficEntrypoint

T0 = datetime(2026, 9, 1, 12, 0)


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


def _node(name, *, zone="dc-1", region="r-1", internal="10.0.0.1", external=None,
          extra_labels=None, unschedulable=False):
    labels = {
        "kubernetes.io/hostname": name,
        m.LABEL_ZONE: zone,
        m.LABEL_REGION: region,
        "beta.kubernetes.io/arch": "amd64",
    }
    labels.update(extra_labels or {})
    addrs = [{"type": "InternalIP", "address": internal}, {"type": "Hostname", "address": name}]
    if external:
        addrs.append({"type": "ExternalIP", "address": external})
    spec = {"unschedulable": True} if unschedulable else {}
    return {"metadata": {"name": name, "labels": labels}, "spec": spec,
            "status": {"addresses": addrs}}


def _ingress(ns, name, hosts, cls="nginx"):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"ingressClassName": cls,
                     "rules": [{"host": h} for h in hosts]}}


def _lb(ns, name, ip):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"type": "LoadBalancer"},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


# --- разбор ноды ----------------------------------------------------------------


def test_parse_node_keeps_only_curated_labels_and_roles():
    p = m.parse_node(_node(
        "node-a", internal="10.0.0.1", external="203.0.113.10",
        extra_labels={"node-role.kubernetes.io/control-plane": "", "env": "prod",
                      "example.test/kingdom": "3"},
        unschedulable=True))
    assert p["name"] == "node-a" and p["zone"] == "dc-1" and p["region"] == "r-1"
    assert p["internal_ip"] == "10.0.0.1" and p["external_ip"] == "203.0.113.10"
    assert p["roles"] == ["control-plane"] and p["unschedulable"] is True
    assert set(p["labels_json"]) == {m.LABEL_ZONE, m.LABEL_REGION, "env", "example.test/kingdom"}
    assert {"type": "Hostname", "address": "node-a"} in p["addresses"]


# --- sync_nodes -------------------------------------------------------------------


def test_sync_nodes_upserts_and_marks_missing_deleted(db):
    with patch.object(m, "_kubectl_get_nodes",
                      return_value=[_node("node-a"), _node("node-b", zone="dc-2")]):
        st = m.sync_nodes(db, now=T0)
    assert st["nodes_fetched"] == 2 and st["created"] == 2 and st["marked_deleted"] == 0

    t1 = T0 + timedelta(minutes=30)
    with patch.object(m, "_kubectl_get_nodes",
                      return_value=[_node("node-a", zone="dc-3")]):
        st = m.sync_nodes(db, now=t1)
    assert st["created"] == 0 and st["marked_deleted"] == 1
    rows = {r.name: r for r in db.query(K8sNode)}
    assert rows["node-a"].zone == "dc-3" and rows["node-a"].last_seen_at == t1
    assert rows["node-a"].first_seen_at == T0 and rows["node-a"].deleted_at is None
    # Пропавшая нода не удаляется — помечается, строка остаётся для истории.
    assert rows["node-b"].deleted_at == t1

    # Вернулась — снова живая.
    with patch.object(m, "_kubectl_get_nodes",
                      return_value=[_node("node-a"), _node("node-b")]):
        m.sync_nodes(db, now=t1 + timedelta(minutes=30))
    assert db.query(K8sNode).filter_by(name="node-b").one().deleted_at is None


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="kubectl", timeout=60),
    OSError("kubectl not found"),
    KubectlCircuitOpen("open"),
])
def test_kubectl_failure_never_marks_nodes_deleted(db, exc):
    with patch.object(m, "_kubectl_get_nodes", return_value=[_node("node-a")]):
        m.sync_nodes(db, now=T0)
    with patch.object(m, "run_kubectl", side_effect=exc):
        st = m.sync_nodes(db, now=T0 + timedelta(hours=1))
    assert st["skipped"] and st["errors"] == 1 and st["marked_deleted"] == 0
    assert db.query(K8sNode).one().deleted_at is None


def test_kubectl_rc_and_empty_answer_do_not_delete(db):
    with patch.object(m, "_kubectl_get_nodes", return_value=[_node("node-a")]):
        m.sync_nodes(db, now=T0)
    bad = SimpleNamespace(returncode=1, stdout="", stderr="Forbidden")
    with patch.object(m, "run_kubectl", return_value=bad):
        assert m.sync_nodes(db)["skipped"]
    # Успешный, но пустой ответ — подозрение, а не «нод больше нет».
    empty = SimpleNamespace(returncode=0, stdout='{"items": []}', stderr="")
    with patch.object(m, "run_kubectl", return_value=empty):
        st = m.sync_nodes(db)
    assert st["nodes_fetched"] == 0 and st["marked_deleted"] == 0
    assert db.query(K8sNode).one().deleted_at is None


# --- sync_entrypoints ---------------------------------------------------------------


def _seed_nodes(db):
    with patch.object(m, "_kubectl_get_nodes", return_value=[
        _node("node-a", internal="10.0.0.1", external="203.0.113.10"),
        _node("node-b", internal="10.0.0.2", external="203.0.113.20"),
    ]):
        m.sync_nodes(db, now=T0)


def _run_entrypoints(db, ingresses, services, dns, now=T0):
    with patch.object(m, "_kubectl_get_ingresses", return_value=ingresses), \
         patch.object(m, "_kubectl_get_services", return_value=services), \
         patch.object(m, "_resolve_host", side_effect=lambda h: dns.get(h, [])):
        return m.sync_entrypoints(db, now=now)


def test_entrypoints_match_node_address_and_lb_service(db):
    _seed_nodes(db)
    st = _run_entrypoints(
        db,
        [_ingress("app-1", "web", ["app.example.test", "*.example.test", ""]),
         _ingress("app-2", "api", ["API.example.test"], cls="nginx-public"),
         _ingress("app-3", "gone", ["nowhere.example.test"])],
        [_lb("ingress", "controller", "203.0.113.10"),
         {"metadata": {"namespace": "x", "name": "cip"}, "spec": {"type": "ClusterIP"}}],
        {"app.example.test": ["203.0.113.10"], "api.example.test": ["198.51.100.7"]},
    )
    rows = {r.host: r for r in db.query(TrafficEntrypoint)}
    # Wildcard и пустой host не резолвятся как имя — их нет.
    assert set(rows) == {"app.example.test", "api.example.test", "nowhere.example.test"}
    app = rows["app.example.test"]
    assert app.entry_nodes == ["node-a"] and app.lb_services == ["ingress/controller"]
    assert app.resolved_ips == ["203.0.113.10"] and app.ingress_classes == ["nginx"]
    # Резолвится мимо нод (например, CDN) — входа через ноду нет.
    api = rows["api.example.test"]
    assert api.entry_nodes == [] and api.lb_services == [] and api.ingress_classes == ["nginx-public"]
    # Не резолвится — строка есть, resolved_ips пуст, синк не упал.
    assert rows["nowhere.example.test"].resolved_ips == []
    assert st["hosts_seen"] == 3 and st["unresolved"] == 1 and st["with_entry_nodes"] == 1


def test_entrypoints_missing_host_marked_deleted_and_fetch_failure_skips(db):
    _seed_nodes(db)
    dns = {"a.example.test": ["203.0.113.20"], "b.example.test": ["203.0.113.20"]}
    _run_entrypoints(db, [_ingress("n", "i", ["a.example.test", "b.example.test"])], [], dns)
    t1 = T0 + timedelta(minutes=30)
    st = _run_entrypoints(db, [_ingress("n", "i", ["a.example.test"])], [], dns, now=t1)
    assert st["marked_deleted"] == 1
    assert db.query(TrafficEntrypoint).filter_by(host="b.example.test").one().deleted_at == t1

    with patch.object(m, "_kubectl_get_ingresses", side_effect=m.NodesFetchError("timeout")):
        st = m.sync_entrypoints(db, now=t1 + timedelta(minutes=30))
    assert st["skipped"] and st["marked_deleted"] == 0
    assert db.query(TrafficEntrypoint).filter_by(host="a.example.test").one().deleted_at is None


def test_resolve_host_failure_is_empty_not_crash():
    import socket

    with patch.object(m.socket, "getaddrinfo", side_effect=socket.gaierror("no such host")):
        assert m._resolve_host("nowhere.example.test") == []
    with patch.object(m.socket, "getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", 0)),
    ]):
        assert m._resolve_hosts(["x.example.test"]) == {"x.example.test": ["198.51.100.1"]}


def test_combined_sync_reports_skipped_only_when_both_fail(db):
    fail = m.NodesFetchError("apiserver down")
    with patch.object(m, "_kubectl_get_nodes", side_effect=fail), \
         patch.object(m, "_kubectl_get_ingresses", side_effect=fail):
        res = m.sync_nodes_and_entrypoints(db)
    assert res["skipped"]
    with patch.object(m, "_kubectl_get_nodes", return_value=[_node("node-a")]), \
         patch.object(m, "_kubectl_get_ingresses", side_effect=fail):
        res = m.sync_nodes_and_entrypoints(db)
    assert "skipped" not in res and res["nodes"]["nodes_fetched"] == 1


def test_beat_task_status_from_counts(db):
    from app.knowledge_graph.source_status import SourceStatus, status_of
    from app.workers import tasks

    with patch.object(tasks, "SessionLocal", return_value=db), \
         patch.object(m, "_kubectl_get_nodes", return_value=[_node("node-a")]), \
         patch.object(m, "_kubectl_get_ingresses", return_value=[]), \
         patch.object(m, "_kubectl_get_services", return_value=[]), \
         patch("app.workers.task_lock._redis_client", return_value=None):
        res = tasks.kg_nodes_sync_task.run()
    assert status_of(res) in (SourceStatus.SUCCESS, SourceStatus.PARTIAL)
