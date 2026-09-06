"""API /kg/incidents: список, карточка, timeline, 404, auth на уровне роутера."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import incidents
from app.auth import User, get_current_user
from app.database import Base, get_db
from app.knowledge_graph.incidents import attach_alert
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent

T0 = datetime(2026, 9, 6, 10, 0, 0)


@pytest.fixture
def db():
    # TestClient исполняет sync-эндпоинты в другом потоке: sqlite in-memory
    # без check_same_thread=False и StaticPool там не виден.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _app(db, user=True) -> TestClient:
    app = FastAPI()
    app.include_router(incidents.router, prefix="/kg/incidents",
                       dependencies=[Depends(get_current_user)])
    app.dependency_overrides[get_db] = lambda: db
    if user:
        app.dependency_overrides[get_current_user] = lambda: User(sub="u1", email="u@x", roles=["viewer"])
    return TestClient(app, raise_server_exceptions=False)


def _seed(db):
    svc = upsert_service(db, namespace="squad-1", name="town-service")
    db.flush()
    db.add(AlertEvent(service_id=svc.id, alertname="HighLatency", severity="warning",
                      fingerprint="fp-1", fired_at=T0))
    db.flush()
    inc = attach_alert(db, namespace="squad-1", service_name="town-service", service_id=svc.id,
                       fired_at=T0, alertname="HighLatency", severity="warning", fingerprint="fp-1")
    other = upsert_service(db, namespace="prod-kingdom1", name="auth")
    db.flush()
    db.add(AlertEvent(service_id=other.id, alertname="Down", severity="critical",
                      fingerprint="fp-2", fired_at=T0 + timedelta(minutes=1)))
    db.flush()
    attach_alert(db, namespace="prod-kingdom1", service_name="auth", service_id=other.id,
                 fired_at=T0 + timedelta(minutes=1), alertname="Down", severity="critical",
                 fingerprint="fp-2")
    db.commit()
    return inc


def test_unauthenticated_is_rejected(db):
    r = _app(db, user=False).get("/kg/incidents")
    assert r.status_code in (401, 403)


def test_list_orders_newest_first_and_filters(db):
    _seed(db)
    c = _app(db)
    body = c.get("/kg/incidents").json()
    assert body["count"] == 2 and body["incidents"][0]["service"] == "auth"
    assert c.get("/kg/incidents", params={"namespace": "squad-1"}).json()["count"] == 1
    assert c.get("/kg/incidents", params={"status": "resolved"}).json()["count"] == 0
    assert c.get("/kg/incidents", params={"status": "bogus"}).status_code == 422


def test_get_incident_with_alerts(db):
    inc = _seed(db)
    body = _app(db).get(f"/kg/incidents/{inc.id}").json()
    assert body["incident_key"] == inc.incident_key
    assert body["alerts"] == [{
        "alertname": "HighLatency", "severity": "warning", "fingerprint": "fp-1",
        "fired_at": "2026-09-06T10:00:00", "resolved_at": None,
    }]


def test_timeline_endpoint(db):
    inc = _seed(db)
    body = _app(db).get(f"/kg/incidents/{inc.id}/timeline").json()
    assert [e["kind"] for e in body["events"]] == ["incident.opened", "alert.fired"]
    assert body["events"][1]["evidence"] == {"epistemic": "observed", "provenance": "kg_alerts"}
    assert body["unknowns"] == []


def test_unknown_incident_is_404(db):
    assert _app(db).get("/kg/incidents/999").status_code == 404
    assert _app(db).get("/kg/incidents/999/timeline").status_code == 404
