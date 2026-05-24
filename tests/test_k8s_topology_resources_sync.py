"""Тесты на Wave 7 / G1.3 declarative Service+Ingress parser.

Покрываем:
- pure helpers (_selector_matches_labels, _extract_ingress_routes,
  _index_deployments_by_ns, _extract_service_meta)
- sync_all_services: создаёт kg_services + edge serves_traffic
  через selector match на Deployment template labels
- sync_all_ingresses_declarative: создаёт synthetic ingress:<name>
  узел и routes_to edges на existing backend Services
- skipped_no_selector / skipped_no_match / skipped_no_backend_match —
  edge cases не плодят фейк-узлы
- kubectl failure → пустой результат, не raise
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.k8s_topology_resources_sync import (
    DISCOVERED_BY_INGRESS, DISCOVERED_BY_SVC, EDGE_ROUTES_TO,
    EDGE_SERVES_TRAFFIC, _extract_ingress_routes, _extract_service_meta,
    _find_matching_deployments, _index_deployments_by_ns,
    _kubectl_get_all, _selector_matches_labels,
    sync_all_ingresses_declarative, sync_all_services,
    sync_topology_resources)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import Service, ServiceEdge  # noqa: F401


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk_service(
    name: str, namespace: str, selector=None, svc_type="ClusterIP", ports=None,
):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "type": svc_type,
            "clusterIP": "10.0.0.1",
            "ports": ports or [{"port": 80, "targetPort": 8080}],
            "selector": selector or {},
        },
    }


def _mk_deployment(name: str, namespace: str, pod_labels=None):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "metadata": {"labels": pod_labels or {"app": name}},
                "spec": {"containers": [{"env": []}]},
            },
        },
    }


def _mk_ingress(name: str, namespace: str, rules=None, default_backend=None):
    spec = {"rules": rules or []}
    if default_backend:
        spec["defaultBackend"] = {"service": {"name": default_backend}}
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


# ── pure helpers ────────────────────────────────────────────────────────────


def test_selector_matches_labels_exact_match():
    assert _selector_matches_labels({"app": "auth"}, {"app": "auth", "tier": "api"})


def test_selector_matches_labels_partial():
    assert _selector_matches_labels(
        {"app": "auth", "tier": "api"},
        {"app": "auth", "tier": "api", "version": "v2"},
    )


def test_selector_matches_labels_mismatch_returns_false():
    assert not _selector_matches_labels({"app": "auth"}, {"app": "town"})


def test_selector_matches_labels_missing_key():
    assert not _selector_matches_labels(
        {"app": "auth", "tier": "api"}, {"app": "auth"},
    )


def test_empty_selector_never_matches():
    """Пустой selector — k8s headless / ExternalName. Не матчит ничего, иначе
    сматчили бы все deployments в namespace."""
    assert not _selector_matches_labels({}, {"app": "town"})
    assert not _selector_matches_labels({}, {})


def test_extract_service_meta():
    svc = _mk_service(
        "auth", "prod-shared",
        selector={"app": "auth"},
        svc_type="LoadBalancer",
        ports=[{"port": 80, "targetPort": 8080, "name": "http"}],
    )
    meta = _extract_service_meta(svc)
    assert meta["service_type"] == "LoadBalancer"
    assert meta["selector"] == {"app": "auth"}
    assert meta["ports"][0]["port"] == 80
    assert meta["cluster_ip"] == "10.0.0.1"


def test_extract_service_meta_missing_spec_safe():
    """Spec может быть None / отсутствовать — не должно raise."""
    meta = _extract_service_meta({"metadata": {"name": "x"}})
    assert meta["service_type"] is None
    assert meta["ports"] == []
    assert meta["selector"] == {}


def test_index_deployments_by_ns():
    deps = [
        _mk_deployment("auth", "prod-shared"),
        _mk_deployment("town", "prod-shared"),
        _mk_deployment("auth", "preprod-shared"),
    ]
    idx = _index_deployments_by_ns(deps)
    assert sorted(idx.keys()) == ["preprod-shared", "prod-shared"]
    assert len(idx["prod-shared"]) == 2
    assert len(idx["preprod-shared"]) == 1


def test_find_matching_deployments_returns_only_matched():
    deps = [
        _mk_deployment("auth", "prod-shared", pod_labels={"app": "auth"}),
        _mk_deployment("town", "prod-shared", pod_labels={"app": "town"}),
        # отличающийся ns — не должен попасть
        _mk_deployment("auth", "preprod-shared", pod_labels={"app": "auth"}),
    ]
    idx = _index_deployments_by_ns(deps)
    out = _find_matching_deployments({"app": "auth"}, "prod-shared", idx)
    assert out == ["auth"]


def test_find_matching_deployments_empty_selector():
    deps = [_mk_deployment("auth", "prod-shared")]
    idx = _index_deployments_by_ns(deps)
    assert _find_matching_deployments({}, "prod-shared", idx) == []


# ── ingress route extraction ────────────────────────────────────────────────


def test_extract_ingress_routes_simple_host_path():
    ing = _mk_ingress("auth-ing", "prod-shared", rules=[{
        "host": "auth.lastoasisgame.com",
        "http": {"paths": [{
            "path": "/api",
            "backend": {"service": {"name": "auth-service"}},
        }]},
    }])
    routes = _extract_ingress_routes(ing)
    assert routes == [("auth.lastoasisgame.com", "/api", "auth-service")]


def test_extract_ingress_routes_default_backend_only():
    ing = _mk_ingress("catchall", "prod-shared", default_backend="fallback-svc")
    routes = _extract_ingress_routes(ing)
    assert (None, "/*", "fallback-svc") in routes


def test_extract_ingress_routes_multiple_hosts():
    ing = _mk_ingress("multi", "prod-shared", rules=[
        {
            "host": "a.example.com",
            "http": {"paths": [{
                "path": "/", "backend": {"service": {"name": "svc-a"}},
            }]},
        },
        {
            "host": "b.example.com",
            "http": {"paths": [{
                "path": "/", "backend": {"service": {"name": "svc-b"}},
            }]},
        },
    ])
    routes = _extract_ingress_routes(ing)
    assert len(routes) == 2
    assert ("a.example.com", "/", "svc-a") in routes
    assert ("b.example.com", "/", "svc-b") in routes


def test_extract_ingress_routes_skips_paths_without_service_name():
    """Path может ссылаться на Resource backend (legacy) вместо Service.
    Тогда service.name отсутствует — пропускаем."""
    ing = _mk_ingress("partial", "prod-shared", rules=[{
        "host": "x.example.com",
        "http": {"paths": [
            {"path": "/", "backend": {"resource": {"name": "static-page"}}},
            {"path": "/api", "backend": {"service": {"name": "api-svc"}}},
        ]},
    }])
    routes = _extract_ingress_routes(ing)
    assert routes == [("x.example.com", "/api", "api-svc")]


def test_extract_ingress_routes_empty_spec():
    assert _extract_ingress_routes({"metadata": {"name": "x"}}) == []


# ── sync_all_services ───────────────────────────────────────────────────────


def test_sync_all_services_creates_node_and_edge(db):
    """Полный happy path: Service со selector матчится на существующий
    Deployment-узел → kg_services upsert + edge serves_traffic."""
    # Pre-existing deployment node (kg_topology_sync уже его создал)
    upsert_service(db, "prod-shared", "auth", team_owner="auth-team")
    db.flush()

    services = [_mk_service("auth", "prod-shared", selector={"app": "auth"})]
    deps = [_mk_deployment("auth", "prod-shared", pod_labels={"app": "auth"})]
    deps_idx = _index_deployments_by_ns(deps)

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=services,
    ):
        stats = sync_all_services(db, deployments_index=deps_idx)

    assert stats["services_fetched"] == 1
    assert stats["nodes_upserted"] == 1
    assert stats["edges_serves_traffic"] == 1

    # Edge действительно лежит в БД
    edges = db.query(ServiceEdge).filter_by(kind=EDGE_SERVES_TRAFFIC).all()
    assert len(edges) == 1
    e = edges[0]
    assert e.src.name == "auth"
    assert e.dst.name == "auth"
    assert e.discovered_by == DISCOVERED_BY_SVC
    assert (e.extras or {}).get("confidence") == "declared_k8s"


def test_sync_all_services_no_selector_creates_node_only(db):
    """Headless service / ExternalName без selector → node OK, edge нет."""
    services = [_mk_service("external-thing", "prod-shared", selector={})]

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=services,
    ):
        stats = sync_all_services(db, deployments_index={})

    assert stats["nodes_upserted"] == 1
    assert stats["skipped_no_selector"] == 1
    assert stats["edges_serves_traffic"] == 0
    assert db.query(ServiceEdge).count() == 0
    # Сам узел всё равно создан
    assert db.query(Service).filter_by(name="external-thing").count() == 1


def test_sync_all_services_skipped_no_match_when_no_deployment(db):
    """Selector задан, но никакой Deployment не матчит. Узел Service есть,
    edge нет, фантом-Deployment не создан."""
    services = [_mk_service("auth", "prod-shared", selector={"app": "auth"})]
    deps = [_mk_deployment("town", "prod-shared", pod_labels={"app": "town"})]

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=services,
    ):
        stats = sync_all_services(
            db, deployments_index=_index_deployments_by_ns(deps),
        )

    assert stats["edges_serves_traffic"] == 0
    assert stats["skipped_no_match"] == 1
    # фантом-Deployment не создан — только Service
    names = {s.name for s in db.query(Service).all()}
    assert names == {"auth"}


def test_sync_all_services_no_edge_when_deployment_has_different_name(db):
    """Selector матчит k8s Deployment с ОТЛИЧАЮЩИМСЯ именем от Service.
    Service-name 'auth-svc', Deployment-name 'auth-app', match по
    label app=auth. Deployment-узла 'auth-app' нет в KG (kg_topology_sync
    ещё не прошёл) → edge не создаётся, фантом не плодим."""
    services = [_mk_service("auth-svc", "prod-shared", selector={"app": "auth"})]
    deps = [_mk_deployment("auth-app", "prod-shared", pod_labels={"app": "auth"})]

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=services,
    ):
        stats = sync_all_services(
            db, deployments_index=_index_deployments_by_ns(deps),
        )

    # Service-node 'auth-svc' создан (upsert).
    assert stats["nodes_upserted"] == 1
    # Selector сматчил Deployment 'auth-app', но узла 'auth-app' нет в KG
    # — edge не создаём, фантом не плодим. kg_topology_sync на следующем
    # часовом тике создаст 'auth-app', потом этот таск свяжет.
    assert stats["edges_serves_traffic"] == 0
    assert db.query(ServiceEdge).count() == 0
    # Только сам Service-узел в kg_services
    names = {s.name for s in db.query(Service).all()}
    assert names == {"auth-svc"}


def test_sync_all_services_idempotent_second_run(db):
    """Повторный sync того же snapshot — не дублирует edges (upsert по
    (src,dst,kind))."""
    upsert_service(db, "prod-shared", "auth")
    db.flush()

    services = [_mk_service("auth", "prod-shared", selector={"app": "auth"})]
    deps = [_mk_deployment("auth", "prod-shared", pod_labels={"app": "auth"})]
    deps_idx = _index_deployments_by_ns(deps)

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=services,
    ):
        sync_all_services(db, deployments_index=deps_idx)
        sync_all_services(db, deployments_index=deps_idx)

    assert db.query(ServiceEdge).filter_by(kind=EDGE_SERVES_TRAFFIC).count() == 1


def test_sync_all_services_empty_when_kubectl_fails(db):
    """kubectl упал → услуг 0, не raise."""
    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=[],
    ):
        stats = sync_all_services(db, deployments_index={})
    assert stats == {
        "services_fetched": 0, "nodes_upserted": 0,
        "edges_serves_traffic": 0,
        "skipped_no_selector": 0, "skipped_no_match": 0,
    }


# ── sync_all_ingresses_declarative ──────────────────────────────────────────


def test_sync_ingresses_creates_routes_to_edges(db):
    """Ingress с двумя hosts → ingress-node + 2 routes_to edges на
    existing services."""
    upsert_service(db, "prod-shared", "auth-service")
    upsert_service(db, "prod-shared", "town-service")
    db.flush()

    ing = _mk_ingress("main-ing", "prod-shared", rules=[
        {
            "host": "auth.x.com",
            "http": {"paths": [{
                "path": "/", "backend": {"service": {"name": "auth-service"}},
            }]},
        },
        {
            "host": "town.x.com",
            "http": {"paths": [{
                "path": "/", "backend": {"service": {"name": "town-service"}},
            }]},
        },
    ])

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=[ing],
    ):
        stats = sync_all_ingresses_declarative(db)

    assert stats["ingresses_fetched"] == 1
    assert stats["routes_seen"] == 2
    assert stats["edges_created"] == 2
    assert stats["skipped_no_backend_match"] == 0

    edges = db.query(ServiceEdge).filter_by(kind=EDGE_ROUTES_TO).all()
    assert len(edges) == 2
    assert {e.dst.name for e in edges} == {"auth-service", "town-service"}
    for e in edges:
        assert e.src.name == "ingress:main-ing"
        assert e.src.synthetic is True
        assert e.discovered_by == DISCOVERED_BY_INGRESS


def test_sync_ingresses_skips_unknown_backend(db):
    """Backend service не существует в KG — edge не создаём (избегаем
    фейк-узлов с одной болтающейся стрелкой)."""
    ing = _mk_ingress("main-ing", "prod-shared", rules=[{
        "host": "x.com",
        "http": {"paths": [{
            "path": "/", "backend": {"service": {"name": "ghost-service"}},
        }]},
    }])

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=[ing],
    ):
        stats = sync_all_ingresses_declarative(db)

    assert stats["skipped_no_backend_match"] == 1
    assert stats["edges_created"] == 0
    # ingress-node всё равно создан (synthetic external) — это нормально,
    # он не учитывается в orphan-метрике
    assert db.query(ServiceEdge).filter_by(kind=EDGE_ROUTES_TO).count() == 0


def test_sync_ingresses_default_backend(db):
    """defaultBackend → один route без host."""
    upsert_service(db, "prod-shared", "fallback-svc")
    db.flush()

    ing = _mk_ingress("catchall", "prod-shared", default_backend="fallback-svc")

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=[ing],
    ):
        stats = sync_all_ingresses_declarative(db)

    assert stats["edges_created"] == 1
    edge = db.query(ServiceEdge).filter_by(kind=EDGE_ROUTES_TO).one()
    assert edge.dst.name == "fallback-svc"
    assert (edge.extras or {}).get("host") == "*"


def test_sync_ingresses_idempotent_second_run(db):
    """Повторный tick — count edges не растёт (UNIQUE(src,dst,kind))."""
    upsert_service(db, "prod-shared", "auth-service")
    db.flush()

    ing = _mk_ingress("main-ing", "prod-shared", rules=[{
        "host": "x.com",
        "http": {"paths": [{
            "path": "/", "backend": {"service": {"name": "auth-service"}},
        }]},
    }])

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        return_value=[ing],
    ):
        sync_all_ingresses_declarative(db)
        sync_all_ingresses_declarative(db)

    assert db.query(ServiceEdge).filter_by(kind=EDGE_ROUTES_TO).count() == 1


# ── orchestrator ────────────────────────────────────────────────────────────


def test_sync_topology_resources_returns_both_slices(db):
    """sync_topology_resources композирует services+ingresses, оба stats
    возвращаются под отдельными ключами."""
    upsert_service(db, "prod-shared", "auth")
    upsert_service(db, "prod-shared", "auth-service")
    db.flush()

    services = [_mk_service("auth", "prod-shared", selector={"app": "auth"})]
    deps = [_mk_deployment("auth", "prod-shared", pod_labels={"app": "auth"})]
    ing = _mk_ingress("main-ing", "prod-shared", rules=[{
        "host": "x.com",
        "http": {"paths": [{
            "path": "/", "backend": {"service": {"name": "auth-service"}},
        }]},
    }])

    # _kubectl_get_all вызывается 3 раза: deployments / services / ingresses
    def fake_kubectl(resource):
        if resource == "deployments":
            return deps
        if resource == "services":
            return services
        if resource == "ingresses":
            return [ing]
        return []

    with patch(
        "app.knowledge_graph.k8s_topology_resources_sync._kubectl_get_all",
        side_effect=fake_kubectl,
    ):
        result = sync_topology_resources(db)

    assert "services" in result and "ingresses" in result
    assert result["services"]["edges_serves_traffic"] == 1
    assert result["ingresses"]["edges_created"] == 1


# ── kubectl wrapper edge cases ──────────────────────────────────────────────


def test_kubectl_get_all_returns_empty_on_nonzero_rc():
    """Если kubectl rc=1 — вернуть [], не raise."""
    class FakeOut:
        returncode = 1
        stdout = ""
        stderr = "no kubeconfig"

    with patch("subprocess.run", return_value=FakeOut()):
        assert _kubectl_get_all("services") == []


def test_kubectl_get_all_returns_empty_on_bad_json():
    class FakeOut:
        returncode = 0
        stdout = "not json"
        stderr = ""

    with patch("subprocess.run", return_value=FakeOut()):
        assert _kubectl_get_all("services") == []


def test_kubectl_get_all_returns_empty_on_timeout():
    import subprocess as _sp

    def _raise(*a, **kw):
        raise _sp.TimeoutExpired(cmd="kubectl", timeout=30)

    with patch("subprocess.run", side_effect=_raise):
        assert _kubectl_get_all("services") == []
