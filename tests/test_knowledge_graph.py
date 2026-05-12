"""Тесты на minimal knowledge graph.

Используем in-memory SQLite — таблицы поднимаются из Base.metadata.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
# Импортируем schema, чтобы ORM-классы зарегистрировались в Base.metadata
from app.knowledge_graph.schema import (AlertEvent, Deployment, Service,
                                        ServiceEdge)  # noqa: F401
from app.knowledge_graph.populator import (record_alert_event,
                                           record_deployment, upsert_edge,
                                           upsert_service)
from app.knowledge_graph.queries import (incidents_on, nearby_alerts,
                                         recent_deploys_for, upstream_of)


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


# ---------- populator -----------------------------------------------------

def test_upsert_service_creates_then_updates(db):
    s1 = upsert_service(db, "squad-1", "town-service", team_owner="gd")
    assert s1.id is not None
    s2 = upsert_service(db, "squad-1", "town-service", team_owner="gd-new")
    assert s1.id == s2.id  # тот же ID, не дубль
    assert s2.team_owner == "gd-new"


def test_upsert_edge_idempotent(db):
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    e1 = upsert_edge(db, a, b, kind="calls", weight=5)
    e2 = upsert_edge(db, a, b, kind="calls", weight=10)
    assert e1.id == e2.id
    assert e2.weight == 10


def test_record_alert_idempotent_by_fingerprint(db):
    svc = upsert_service(db, "squad-1", "x")
    fired = datetime(2026, 5, 12, 10, 0)
    a1 = record_alert_event(
        db, svc, "HighLatency", "warning", "fp-1", fired
    )
    a2 = record_alert_event(
        db, svc, "HighLatency", "critical", "fp-1", fired
    )
    assert a1.id == a2.id
    assert a2.severity == "critical"


# ---------- queries -------------------------------------------------------

def test_recent_deploys_within_window(db):
    svc = upsert_service(db, "squad-1", "town-service")
    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    # деплой за 15 мин до инцидента
    record_deployment(
        db, svc,
        started_at=incident_at.replace(tzinfo=None) - timedelta(minutes=15),
        sha="abc",
    )
    # деплой за 8 часов — не должен попасть
    record_deployment(
        db, svc,
        started_at=incident_at.replace(tzinfo=None) - timedelta(hours=8),
        sha="old",
    )

    deploys = recent_deploys_for(
        db, "squad-1", "town-service", before=incident_at, lookback_minutes=60
    )
    assert len(deploys) == 1
    assert deploys[0]["sha"] == "abc"
    assert deploys[0]["minutes_before_incident"] == 15


def test_recent_deploys_unknown_service(db):
    """Сервиса в графе нет — возвращаем [] (≠ ошибка)."""
    out = recent_deploys_for(
        db, "squad-1", "nope", before=datetime.now(timezone.utc)
    )
    assert out == []


def test_upstream_of(db):
    api = upsert_service(db, "squad-1", "api")
    auth = upsert_service(db, "squad-1", "auth")
    db_svc = upsert_service(db, "squad-1", "postgres")
    upsert_edge(db, api, auth, kind="calls", weight=10)  # api → auth
    upsert_edge(db, api, db_svc, kind="reads_from", weight=5)  # api → postgres

    # upstream(api) = от кого api зависит = auth и postgres.
    upstream = upstream_of(db, "squad-1", "api")
    services = {u["service"] for u in upstream}
    assert services == {"auth", "postgres"}


def test_upstream_of_filtered_by_kind(db):
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    upsert_edge(db, a, b, kind="calls")
    upsert_edge(db, a, b, kind="reads_from")

    # upstream(a) с фильтром по calls — только b через calls.
    only_calls = upstream_of(db, "squad-1", "a", kinds=["calls"])
    assert len(only_calls) == 1
    assert only_calls[0]["service"] == "b"


def test_incidents_on_window(db):
    svc = upsert_service(db, "squad-1", "x")
    base = datetime(2026, 5, 12, 10, 0)
    record_alert_event(db, svc, "A1", "warning", "fp-a", base)
    record_alert_event(db, svc, "A2", "critical", "fp-b", base + timedelta(minutes=5))
    record_alert_event(db, svc, "OLD", "warning", "fp-c", base - timedelta(hours=24))

    rows = incidents_on(
        db, "squad-1", "x",
        since=base - timedelta(minutes=10),
        until=base + timedelta(minutes=30),
    )
    fp = {r["fingerprint"] for r in rows}
    assert fp == {"fp-a", "fp-b"}


def test_nearby_alerts_finds_upstream_in_window(db):
    """Боевой кейс: alert на town-service, ищем upstream-инциденты в ±15 мин."""
    town = upsert_service(db, "squad-1", "town-service")
    auth = upsert_service(db, "squad-1", "auth")
    db_svc = upsert_service(db, "squad-1", "postgres")
    # town вызывает auth и читает из postgres
    upsert_edge(db, town, auth, kind="calls", weight=10)
    upsert_edge(db, town, db_svc, kind="reads_from", weight=5)

    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    # auth упал за 3 минуты до инцидента town
    record_alert_event(
        db, auth, "AuthLatency", "warning", "fp-auth",
        incident_at.replace(tzinfo=None) - timedelta(minutes=3),
    )
    # postgres — был alert за 30 минут (вне окна ±15)
    record_alert_event(
        db, db_svc, "DiskFull", "critical", "fp-pg",
        incident_at.replace(tzinfo=None) - timedelta(minutes=30),
    )

    nearby = nearby_alerts(
        db, "squad-1", "town-service", around=incident_at, window_minutes=15
    )
    services = {n["service"] for n in nearby}
    assert services == {"auth"}  # postgres вне окна
    assert nearby[0]["minutes_before"] == 3
    assert nearby[0]["edge_kind"] == "calls"


def test_nearby_alerts_no_upstream(db):
    """Сервис без upstream — пустой результат."""
    svc = upsert_service(db, "squad-1", "isolated")
    out = nearby_alerts(
        db, "squad-1", "isolated", around=datetime.now(timezone.utc)
    )
    assert out == []
