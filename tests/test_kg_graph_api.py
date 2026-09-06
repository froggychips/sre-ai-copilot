"""API /kg/blast-radius: параметры, 404, auth на уровне роутера."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import kg_graph
from app.auth import User, get_current_user
from app.database import Base, get_db
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Service(id=1, namespace="ns", name="town", node_kind=NODE_KIND_SERVICE))
    s.add(Service(id=2, namespace="ns", name="town", node_kind=NODE_KIND_WORKLOAD))
    s.add(Service(id=3, namespace="ns", name="auth", node_kind=NODE_KIND_SERVICE))
    s.add(ServiceEdge(src_id=3, dst_id=1, kind="calls", extras={"discovery_sources": ["kg_sync/env_vars"]},
                      discovered_by="kg_sync/env_vars"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _client(db, user=True):
    app = FastAPI()
    app.include_router(kg_graph.router, prefix="/kg", dependencies=[Depends(get_current_user)])
    app.dependency_overrides[get_db] = lambda: db
    if user:
        app.dependency_overrides[get_current_user] = lambda: User(sub="u", email="u@x", roles=[])
    return TestClient(app, raise_server_exceptions=False)


def test_requires_auth(db):
    assert _client(db, user=False).get("/kg/blast-radius", params={"namespace": "ns", "service": "town"}).status_code in (401, 403)


def test_returns_impact_with_evidence(db):
    body = _client(db).get("/kg/blast-radius", params={"namespace": "ns", "service": "town"}).json()
    assert body["target"] == {"namespace": "ns", "name": "town", "node_kind": "service"}
    assert [e["service"] for e in body["impact"]] == ["auth"]
    assert body["impact"][0]["epistemic"] == "inferred"
    assert body["unknowns"][0]["scope"] == "callers"


def test_missing_params_and_unknown_service(db):
    c = _client(db)
    assert c.get("/kg/blast-radius").status_code == 422
    assert c.get("/kg/blast-radius", params={"namespace": "ns", "service": "ghost"}).status_code == 404
    assert c.get("/kg/blast-radius", params={"namespace": "ns", "service": "town", "hops": 9}).status_code == 422
