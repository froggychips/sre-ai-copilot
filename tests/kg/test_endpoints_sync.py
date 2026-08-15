"""Endpoints: за Service либо стоят живые поды, либо это надо видеть.

`serves_traffic` строится по совпадению `Service.spec.selector` с labels
workload'а — то есть отвечает «должен ли Service туда маршрутизировать», а не
«делает ли». Endpoints отвечают на второй вопрос: контроллер kubernetes
записывает адреса реально готовых подов.

Замер 15.08.2026: 4732 Service с адресами, 83 без. Среди пустых — ни одного
headless, ни одного ExternalName, ни одного без селектора. Все 83 обязаны
кого-то обслуживать и не обслуживают никого.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import k8s_endpoints_sync as eps
from app.knowledge_graph.confidence import confidence_score
from app.knowledge_graph.k8s_endpoints_sync import (DISCOVERED_BY_ENDPOINTS,
                                                    EndpointsFetchError,
                                                    sync_endpoints)
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)

NS = "prod-kingdom1"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Service(id=1, namespace=NS, name="auth", node_kind=NODE_KIND_SERVICE))
    s.add(Service(id=2, namespace=NS, name="auth", node_kind=NODE_KIND_WORKLOAD))
    s.commit()
    return s


@pytest.fixture
def cluster(monkeypatch):
    """Подменяет ответ kubectl: {(ns, name): число готовых адресов}."""
    def _install(mapping):
        items = []
        for (ns, name), ready in mapping.items():
            subsets = ([{"addresses": [{"ip": f"10.0.0.{i}"} for i in range(ready)]}]
                       if ready else [])
            items.append({"metadata": {"namespace": ns, "name": name},
                          "subsets": subsets})
        monkeypatch.setattr(eps, "_fetch_endpoints", lambda: items)
    return _install


# --- живые поды -----------------------------------------------------------


def test_ready_addresses_are_recorded_on_the_node(db, cluster):
    cluster({(NS, "auth"): 3})
    stats = sync_endpoints(db, now=datetime(2026, 8, 15, 12, 0))

    node = db.query(Service).filter_by(name="auth", node_kind=NODE_KIND_SERVICE).one()
    assert node.metadata_json["endpoints_ready"] == 3
    assert node.metadata_json["endpoints_checked_at"].startswith("2026-08-15")
    assert stats["with_pods"] == 1 and stats["empty"] == 0


def test_existing_edge_gets_a_second_source(db, cluster):
    """Endpoints подтверждают ребро, а не создают своё.

    Два независимых источника на одном ребре поднимают его достоверность —
    это и есть смысл corroboration в confidence_score.
    """
    db.add(ServiceEdge(src_id=1, dst_id=2, kind="serves_traffic",
                       last_seen_at=datetime.utcnow(),
                       extras={"discovery_sources": ["k8s_topology_resources/service"]}))
    db.commit()
    before = confidence_score(db.query(ServiceEdge).one().extras, datetime.utcnow())

    cluster({(NS, "auth"): 2})
    sync_endpoints(db)

    edge = db.query(ServiceEdge).one()
    assert DISCOVERED_BY_ENDPOINTS in edge.extras["discovery_sources"]
    assert confidence_score(edge.extras, datetime.utcnow()) > before


def test_no_new_edges_are_invented(db, cluster):
    """Кого именно обслуживает Service — знает топологический синк."""
    cluster({(NS, "auth"): 2})
    sync_endpoints(db)
    assert db.query(ServiceEdge).count() == 0


# --- пустые Service -------------------------------------------------------


def test_service_without_pods_is_reported_by_name(db, cluster):
    """«83 пустых Service» без имён — метрика, по ней нельзя ничего сделать."""
    cluster({(NS, "auth"): 0})
    stats = sync_endpoints(db)

    assert stats["empty"] == 1
    assert stats["empty_services"] == [f"{NS}/auth"]
    node = db.query(Service).filter_by(name="auth", node_kind=NODE_KIND_SERVICE).one()
    assert node.metadata_json["endpoints_ready"] == 0


def test_empty_service_does_not_corroborate_edges(db, cluster):
    """Ребро к Service без подов не должно становиться достовернее."""
    db.add(ServiceEdge(src_id=1, dst_id=2, kind="serves_traffic",
                       last_seen_at=datetime.utcnow(),
                       extras={"discovery_sources": ["k8s_topology_resources/service"]}))
    db.commit()

    cluster({(NS, "auth"): 0})
    sync_endpoints(db)

    edge = db.query(ServiceEdge).one()
    assert DISCOVERED_BY_ENDPOINTS not in edge.extras["discovery_sources"]


# --- deadman --------------------------------------------------------------


def test_empty_cluster_response_is_a_failure(monkeypatch):
    """Кластер без единого endpoints невозможен — у kube-system они есть всегда.

    Без этого один неудачный тик пометил бы КАЖДЫЙ Service кластера как
    оставшийся без подов.
    """
    class R:
        returncode = 0
        stdout = '{"items": []}'
        stderr = ""

    monkeypatch.setattr(eps.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(EndpointsFetchError):
        eps._fetch_endpoints()


def test_kubectl_failure_changes_nothing(db, monkeypatch):
    db.add(ServiceEdge(src_id=1, dst_id=2, kind="serves_traffic",
                       last_seen_at=datetime.utcnow(), extras={}))
    db.commit()

    def boom():
        raise EndpointsFetchError("kubectl недоступен")

    monkeypatch.setattr(eps, "_fetch_endpoints", boom)
    with pytest.raises(EndpointsFetchError):
        sync_endpoints(db)

    node = db.query(Service).filter_by(name="auth", node_kind=NODE_KIND_SERVICE).one()
    assert not (node.metadata_json or {}).get("endpoints_checked_at")


# --- границы --------------------------------------------------------------


def test_unknown_service_is_left_alone(db, cluster):
    """Узел, которого нет в ответе, не трогаем: его Service мог быть удалён."""
    cluster({(NS, "другой-сервис"): 1})
    stats = sync_endpoints(db)

    assert stats["matched"] == 0
    node = db.query(Service).filter_by(name="auth", node_kind=NODE_KIND_SERVICE).one()
    assert node.metadata_json is None


def test_not_ready_addresses_do_not_count(db, cluster, monkeypatch):
    """Под без readiness трафик не получает — значит Service никого не обслуживает."""
    monkeypatch.setattr(eps, "_fetch_endpoints", lambda: [{
        "metadata": {"namespace": NS, "name": "auth"},
        "subsets": [{"notReadyAddresses": [{"ip": "10.0.0.1"}]}],
    }])
    stats = sync_endpoints(db)
    assert stats["empty"] == 1
