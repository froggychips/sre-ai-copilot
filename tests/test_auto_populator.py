"""Тесты на auto_populator — наполнение KG из инцидентов."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.queries import (incidents_on, nearby_alerts,
                                         recent_deploys_for)
from app.knowledge_graph.schema import (AlertEvent, Service,
                                        ServiceEdge)  # noqa: F401
from app.models.incident import Incident


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _incident(
    incident_id="inc-1", service="town-service", namespace="squad-1",
    starts_at="2026-05-12T10:00:00Z", tc=None,
):
    return Incident(
        incident_id=incident_id,
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace=namespace,
        labels={"alertname": "HighLatency", "service": service,
                "severity": "warning"},
        annotations={},
        starts_at=starts_at,
        teamcity_context=tc,
    )


def test_populate_creates_service_and_alert(db):
    stats = populate_from_incident(db, _incident())
    assert stats["services_touched"] == 1
    assert stats["alerts_added"] == 1
    assert stats["deploys_added"] == 0

    svc = db.query(Service).filter(Service.name == "town-service").one()
    assert svc.namespace == "squad-1"

    alerts = incidents_on(
        db, "squad-1", "town-service",
        since=datetime(2026, 5, 12, 9, 0),
        until=datetime(2026, 5, 12, 11, 0),
    )
    assert len(alerts) == 1
    assert alerts[0]["fingerprint"] == "inc-1"


def test_populate_records_deployments_from_tc(db):
    tc = {
        "recent_builds": [
            {
                "number": "1234", "status": "SUCCESS",
                "buildtype_id": "Wo_Backend_Town",
                "branch": "master",
                "finished_at": "2026-05-12T09:30:00Z",
                "started_at": "2026-05-12T09:25:00Z",
                "sha": "abc1234",
            },
            {
                "number": "1235", "status": "RUNNING",
                "buildtype_id": "Wo_Backend_Town",
                "started_at": "2026-05-12T09:55:00Z",
            },
        ]
    }
    stats = populate_from_incident(db, _incident(tc=tc))
    assert stats["deploys_added"] == 2

    deploys = recent_deploys_for(
        db, "squad-1", "town-service",
        before=datetime(2026, 5, 12, 10, 0),
        lookback_minutes=120,
    )
    shas = {d["sha"] for d in deploys}
    assert "abc1234" in shas


def test_populate_skips_when_no_service_label(db):
    incident = Incident(
        incident_id="inc-noservice",
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace="squad-1",
        labels={"alertname": "GenericAlert"},  # ни service, ни app, ни deployment
        annotations={},
        starts_at="2026-05-12T10:00:00Z",
    )
    stats = populate_from_incident(db, incident)
    assert stats == {"services_touched": 0, "deploys_added": 0, "alerts_added": 0}


def test_populate_idempotent_on_second_call(db):
    """Повторный прогон не дублирует service и не дублирует AlertEvent."""
    populate_from_incident(db, _incident())
    populate_from_incident(db, _incident())  # тот же fingerprint

    assert db.query(Service).count() == 1
    assert db.query(AlertEvent).count() == 1


def test_populate_picks_app_label_when_no_service(db):
    """Если labels.service нет, но есть labels.app — используем его как service-name."""
    incident = Incident(
        incident_id="inc-app",
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace="squad-1",
        labels={"alertname": "X", "app": "auth-svc"},
        annotations={},
        starts_at="2026-05-12T10:00:00Z",
    )
    populate_from_incident(db, incident)
    assert db.query(Service).filter(Service.name == "auth-svc").one() is not None


def test_populate_after_full_cycle_enables_nearby_alerts(db):
    """E2E: 2 инцидента на разные сервисы + edge между ними → nearby_alerts работает."""
    populate_from_incident(db, _incident(
        incident_id="auth-down", service="auth", namespace="squad-1",
        starts_at="2026-05-12T09:57:00Z",
    ))
    populate_from_incident(db, _incident(
        incident_id="town-down", service="town-service", namespace="squad-1",
        starts_at="2026-05-12T10:00:00Z",
    ))

    # Создаём edge town → auth (вручную, потому что populator edges не делает).
    from app.knowledge_graph.populator import upsert_edge
    town = db.query(Service).filter(Service.name == "town-service").one()
    auth = db.query(Service).filter(Service.name == "auth").one()
    upsert_edge(db, town, auth, kind="calls")

    nearby = nearby_alerts(
        db, "squad-1", "town-service",
        around=datetime(2026, 5, 12, 10, 0),
        window_minutes=15,
    )
    assert len(nearby) == 1
    assert nearby[0]["service"] == "auth"
    assert nearby[0]["minutes_before"] == 3
