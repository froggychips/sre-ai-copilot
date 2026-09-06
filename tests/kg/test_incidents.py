"""kg_incidents: детерминированный инцидент из алертов сервиса.

Правило одно: у (namespace, service) не больше одного открытого инцидента;
алерт сервиса присоединяется к нему, переоткрывает недавно закрытый или
заводит новый. Закрытие — когда все алерты resolved; старение — когда давно
ничего не происходит.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.incidents import (AGE_OUT_HOURS, REOPEN_WINDOW_MIN,
                                           RESOLVE_AGED_OUT, RESOLVE_ALL_ALERTS,
                                           STATUS_OPEN, STATUS_RESOLVED,
                                           attach_alert, open_incident_for,
                                           reconcile_incidents)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent, KGIncident
from app.models.incident import Incident

T0 = datetime(2026, 9, 6, 10, 0, 0)


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


def _svc(db, ns="squad-1", name="town-service"):
    svc = upsert_service(db, namespace=ns, name=name)
    db.flush()
    return svc


def _alert(db, svc, fp, fired_at, alertname="HighLatency", severity="warning", resolved_at=None):
    ev = AlertEvent(service_id=svc.id, alertname=alertname, severity=severity,
                    fingerprint=fp, fired_at=fired_at, resolved_at=resolved_at)
    db.add(ev)
    db.flush()
    return ev


def _attach(db, svc, fp, fired_at, alertname="HighLatency", severity="warning"):
    _alert(db, svc, fp, fired_at, alertname, severity)
    return attach_alert(db, namespace=svc.namespace, service_name=svc.name, service_id=svc.id,
                        fired_at=fired_at, alertname=alertname, severity=severity, fingerprint=fp)


# ── открытие и присоединение ──────────────────────────────────────────────

def test_first_alert_opens_incident_and_links_alert_row(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)
    assert inc.status == STATUS_OPEN
    assert inc.incident_key == "squad-1/town-service@20260906T100000"
    assert inc.opened_at == inc.last_alert_at == T0
    assert inc.alert_count == 1 and inc.fingerprints == ["fp-1"] and inc.alertnames == ["HighLatency"]
    assert inc.service_id == svc.id
    row = db.query(AlertEvent).filter_by(fingerprint="fp-1").one()
    assert row.incident_id == inc.incident_key


def test_second_alert_of_same_service_joins_open_incident(db):
    svc = _svc(db)
    first = _attach(db, svc, "fp-1", T0, "HighLatency", "warning")
    second = _attach(db, svc, "fp-2", T0 + timedelta(minutes=7), "PodCrashLooping", "critical")
    assert second.id == first.id
    assert second.alert_count == 2
    assert second.severity == "critical"            # максимум по алертам
    assert second.alertnames == ["HighLatency", "PodCrashLooping"]
    assert second.last_alert_at == T0 + timedelta(minutes=7)
    assert second.opened_at == T0
    assert db.query(KGIncident).count() == 1


def test_refire_of_same_fingerprint_is_idempotent(db):
    svc = _svc(db)
    _attach(db, svc, "fp-1", T0)
    inc = attach_alert(db, namespace=svc.namespace, service_name=svc.name, service_id=svc.id,
                       fired_at=T0 + timedelta(minutes=1), alertname="HighLatency",
                       severity="warning", fingerprint="fp-1")
    assert inc.alert_count == 1 and inc.fingerprints == ["fp-1"]


def test_other_service_gets_its_own_incident(db):
    a = _svc(db, name="town-service")
    b = _svc(db, name="map-service")
    ia = _attach(db, a, "fp-a", T0)
    ib = _attach(db, b, "fp-b", T0)
    assert ia.id != ib.id
    assert open_incident_for(db, "squad-1", "map-service").id == ib.id


def test_late_alert_extends_opened_at_but_keeps_key(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)
    key = inc.incident_key
    inc = _attach(db, svc, "fp-0", T0 - timedelta(minutes=5))
    assert inc.opened_at == T0 - timedelta(minutes=5)
    assert inc.incident_key == key       # на ключ уже ссылаются kg_alerts


def test_one_open_incident_per_service_is_enforced_by_the_index(db):
    db.add(KGIncident(incident_key="k1", namespace="ns", service_name="svc", status=STATUS_OPEN,
                      opened_at=T0, last_alert_at=T0))
    db.flush()
    db.add(KGIncident(incident_key="k2", namespace="ns", service_name="svc", status=STATUS_OPEN,
                      opened_at=T0, last_alert_at=T0))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_resolved_incidents_do_not_block_the_index(db):
    db.add(KGIncident(incident_key="k1", namespace="ns", service_name="svc", status=STATUS_RESOLVED,
                      opened_at=T0, last_alert_at=T0, resolved_at=T0))
    db.add(KGIncident(incident_key="k2", namespace="ns", service_name="svc", status=STATUS_OPEN,
                      opened_at=T0, last_alert_at=T0))
    db.flush()      # частичный индекс: resolved не считается


# ── закрытие и старение ──────────────────────────────────────────────────

def test_reconcile_resolves_when_all_alerts_resolved(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)
    _attach(db, svc, "fp-2", T0 + timedelta(minutes=2))
    for fp, at in (("fp-1", T0 + timedelta(minutes=20)), ("fp-2", T0 + timedelta(minutes=25))):
        db.query(AlertEvent).filter_by(fingerprint=fp).update({"resolved_at": at})
    db.flush()
    stats = reconcile_incidents(db, now=T0 + timedelta(minutes=30))
    db.refresh(inc)
    assert inc.status == STATUS_RESOLVED
    assert inc.resolve_reason == RESOLVE_ALL_ALERTS
    assert inc.resolved_at == T0 + timedelta(minutes=25)
    assert stats == {"checked": 1, "still_open": 0, "resolved": 1, "aged_out": 0}


def test_reconcile_keeps_incident_open_while_any_alert_fires(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)
    _attach(db, svc, "fp-2", T0)
    db.query(AlertEvent).filter_by(fingerprint="fp-1").update({"resolved_at": T0 + timedelta(minutes=5)})
    db.flush()
    stats = reconcile_incidents(db, now=T0 + timedelta(minutes=10))
    db.refresh(inc)
    assert inc.status == STATUS_OPEN and stats["still_open"] == 1


def test_reconcile_ages_out_abandoned_incident(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)          # алерт так и не resolved (AM был недоступен)
    now = T0 + timedelta(hours=AGE_OUT_HOURS + 1)
    stats = reconcile_incidents(db, now=now)
    db.refresh(inc)
    assert inc.status == STATUS_RESOLVED and inc.resolve_reason == RESOLVE_AGED_OUT
    assert inc.resolved_at == now and stats["aged_out"] == 1


def test_reconcile_does_not_age_out_recent_incident(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-1", T0)
    reconcile_incidents(db, now=T0 + timedelta(hours=AGE_OUT_HOURS - 1))
    db.refresh(inc)
    assert inc.status == STATUS_OPEN


# ── переоткрытие (флаппинг) ──────────────────────────────────────────────

def _resolved_incident(db, svc, resolved_at):
    inc = _attach(db, svc, "fp-1", T0)
    db.query(AlertEvent).filter_by(fingerprint="fp-1").update({"resolved_at": resolved_at})
    db.flush()
    reconcile_incidents(db, now=resolved_at)
    db.refresh(inc)
    assert inc.status == STATUS_RESOLVED
    return inc


def test_alert_shortly_after_resolve_reopens_the_same_incident(db):
    svc = _svc(db)
    inc = _resolved_incident(db, svc, T0 + timedelta(minutes=10))
    again = _attach(db, svc, "fp-3", T0 + timedelta(minutes=10 + REOPEN_WINDOW_MIN - 1))
    assert again.id == inc.id
    assert again.status == STATUS_OPEN and again.resolved_at is None and again.resolve_reason is None
    assert again.reopened_count == 1
    assert again.alert_count == 2


def test_alert_after_reopen_window_starts_a_new_incident(db):
    svc = _svc(db)
    inc = _resolved_incident(db, svc, T0 + timedelta(minutes=10))
    fresh = _attach(db, svc, "fp-3", T0 + timedelta(minutes=10 + REOPEN_WINDOW_MIN + 1))
    assert fresh.id != inc.id
    assert fresh.incident_key != inc.incident_key
    assert db.query(KGIncident).count() == 2


# ── интеграция с приёмом алерта ──────────────────────────────────────────

def _incoming(incident_id, starts_at, alertname="HighLatency", severity="warning"):
    return Incident(
        incident_id=incident_id, severity=severity, status="firing", summary="x", description="y",
        namespace="squad-1",
        labels={"alertname": alertname, "service": "town-service", "severity": severity},
        annotations={}, starts_at=starts_at,
    )


def test_populate_from_incident_creates_incident_and_rewrites_alert_incident_id(db):
    stats = populate_from_incident(db, _incoming("fp-x", "2026-09-06T10:00:00Z"))
    inc = db.query(KGIncident).one()
    assert stats["kg_incident_id"] == inc.id
    assert inc.namespace == "squad-1" and inc.service_name == "town-service"
    row = db.query(AlertEvent).filter_by(fingerprint="fp-x").one()
    assert row.incident_id == inc.incident_key       # а не копия fingerprint


def test_populate_two_alerts_one_incident(db):
    populate_from_incident(db, _incoming("fp-x", "2026-09-06T10:00:00Z"))
    populate_from_incident(db, _incoming("fp-y", "2026-09-06T10:05:00Z", "PodCrashLooping", "critical"))
    inc = db.query(KGIncident).one()
    assert inc.alert_count == 2 and inc.severity == "critical"
