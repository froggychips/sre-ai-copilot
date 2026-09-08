"""События внешних исполнителей (squad-medic) в графе.

07.09.2026: на ImagePullBackOff squad-39 медик применил 13 grant-фиксов и
запинговал владельца, копилот в тот же час выложил карточку по тому же стенду
— и ни одной общей записи. Здесь: подпись вебхука (fail-closed), запись
события с привязкой к открытому инциденту, событие в timeline инцидента.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.incident_timeline import build_timeline
from app.knowledge_graph.remediation_events import (SIGNATURE_HEADER, TIMESTAMP_HEADER,
                                                    SignatureError,
                                                    check_remediation_signature,
                                                    open_incident_for_namespaces,
                                                    record_external_remediation)
from app.knowledge_graph.schema import KGIncident, KGRemediationEvent
from app.models.remediation_event import RemediationEventIn

SECRET = "medic-secret"
NOW = datetime(2026, 9, 8, 4, 30)


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


def _sign(body: bytes, ts: str, secret: str = SECRET) -> dict:
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: f"sha256={sig}", TIMESTAMP_HEADER: ts}


# --- подпись -------------------------------------------------------------------


def test_signature_ok_returns_hex():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    sig = check_remediation_signature(_sign(body, ts), body, secret=SECRET, max_age_seconds=300)
    assert len(sig) == 64 and not sig.startswith("sha256=")


def test_signature_headers_are_case_insensitive():
    body = b"{}"
    ts = str(int(time.time()))
    headers = {k.lower(): v for k, v in _sign(body, ts).items()}
    assert check_remediation_signature(headers, body, secret=SECRET, max_age_seconds=300)


@pytest.mark.parametrize("mutate,reason", [
    (lambda h, b: (h, b + b" "), "invalid signature"),
    (lambda h, b: ({**h, SIGNATURE_HEADER: "sha256=" + "0" * 64}, b), "invalid signature"),
    (lambda h, b: ({k: v for k, v in h.items() if k != SIGNATURE_HEADER}, b), "missing signature"),
    (lambda h, b: ({k: v for k, v in h.items() if k != TIMESTAMP_HEADER}, b), "missing timestamp"),
])
def test_signature_rejections(mutate, reason):
    body = b'{"squad":"squad-39"}'
    headers, body2 = mutate(_sign(body, str(int(time.time()))), body)
    with pytest.raises(SignatureError, match=reason):
        check_remediation_signature(headers, body2, secret=SECRET, max_age_seconds=300)


def test_stale_timestamp_is_rejected():
    body = b"{}"
    old = str(int(time.time()) - 3600)
    with pytest.raises(SignatureError, match="stale timestamp"):
        check_remediation_signature(_sign(body, old), body, secret=SECRET, max_age_seconds=300)


def test_no_secret_is_fail_closed():
    body = b"{}"
    with pytest.raises(SignatureError, match="not configured"):
        check_remediation_signature(_sign(body, "1"), body, secret=None, max_age_seconds=300)
    with pytest.raises(SignatureError, match="not configured"):
        check_remediation_signature(_sign(body, "1"), body, secret="", max_age_seconds=300)


def test_wrong_secret_is_rejected():
    body = b"{}"
    ts = str(int(time.time()))
    with pytest.raises(SignatureError, match="invalid signature"):
        check_remediation_signature(_sign(body, ts, secret="other"), body, secret=SECRET, max_age_seconds=300)


# --- запись -----------------------------------------------------------------------


def _payload(**over) -> RemediationEventIn:
    base = {
        "actor": "squad-medic", "run_id": "mcp-squad-medic-29814030", "squad": "squad-39",
        "namespace": "squad-39-shared", "namespaces": ["squad-39-shared", "squad-39-kingdom2"],
        "started_at": NOW.isoformat(), "finished_at": (NOW + timedelta(minutes=1)).isoformat(),
        "duration_min": 974, "outcome": "partial", "severity": "blocker",
        "fixed": True, "still_unhealthy": True,
        "applied": ["grants: squad-39-shared/town-db"], "manual": ["image-pull — тег отсутствует в реестре"],
        "gaps": [], "summary": "ImagePullBackOff во всех сервисах",
        "root_cause": "тег снесён retention Nexus", "next_action": "перезапустить BuildAndUpdate",
        "escalated": True, "owner_login": "wizaryx",
    }
    base.update(over)
    return RemediationEventIn(**base)


def _incident(ns="squad-39-kingdom2", svc="town-service", opened=NOW - timedelta(hours=14),
              status="open", resolved=None) -> KGIncident:
    return KGIncident(incident_key=f"{ns}/{svc}@{opened.isoformat()}", namespace=ns, service_name=svc,
                      status=status, severity="warning", opened_at=opened, last_alert_at=opened,
                      resolved_at=resolved, alert_count=1, alertnames=["KubeContainerWaiting"],
                      fingerprints=["fp-1"])


def test_record_links_open_incident_on_any_squad_namespace(db):
    inc = _incident()
    db.add(inc)
    db.commit()

    res = record_external_remediation(db, _payload())

    row = db.query(KGRemediationEvent).one()
    assert res["created"] is True and res["incident_id"] == inc.id
    assert row.incident_id == inc.id
    assert row.service_name == "town-service", "сервис берётся у инцидента, если исполнитель его не назвал"
    assert row.namespaces == ["squad-39-shared", "squad-39-kingdom2"]
    assert (row.outcome, row.fixed, row.still_unhealthy, row.escalated) == ("partial", True, True, True)
    assert row.next_action == "перезапустить BuildAndUpdate"


def test_record_without_incident_and_duplicate_run_is_idempotent(db):
    first = record_external_remediation(db, _payload())
    second = record_external_remediation(db, _payload(summary="повтор"))
    assert first["created"] is True and first["incident_id"] is None
    assert second["created"] is False and second["id"] == first["id"]
    assert db.query(KGRemediationEvent).count() == 1


def test_open_incident_lookup_respects_time(db):
    resolved_before = _incident(svc="a", opened=NOW - timedelta(hours=5), status="resolved",
                                resolved=NOW - timedelta(hours=4))
    opened_after = _incident(svc="b", opened=NOW + timedelta(hours=1))
    still_open_then = _incident(svc="c", opened=NOW - timedelta(hours=2), status="resolved",
                                resolved=NOW + timedelta(hours=1))
    db.add_all([resolved_before, opened_after, still_open_then])
    db.commit()

    found = open_incident_for_namespaces(db, ["squad-39-shared", "squad-39-kingdom2"], at=NOW)
    assert found is not None and found.service_name == "c"
    assert open_incident_for_namespaces(db, ["other-ns"], at=NOW) is None


# --- timeline ----------------------------------------------------------------------


def test_timeline_shows_external_action_and_refines_known_unknown(db):
    inc = _incident(opened=NOW - timedelta(minutes=30))
    db.add(inc)
    db.commit()
    record_external_remediation(db, _payload())

    tl = build_timeline(db, inc, now=NOW + timedelta(minutes=10))

    ext = [e for e in tl["events"] if e["kind"] == "remediation.external"]
    assert len(ext) == 1
    ev = ext[0]
    assert ev["title"].startswith("squad-medic: partial")
    assert ev["evidence"] == {"epistemic": "observed", "provenance": "kg_remediation_events"}
    assert ev["details"]["next_action"] == "перезапустить BuildAndUpdate"
    assert ev["details"]["manual"] == ["image-pull — тег отсутствует в реестре"]
    assert tl["memory"]["external_actions"] == 1
    # копилот сам не действовал — это остаётся Known Unknown, но уточнённый
    reasons = " ".join(u["reason"] for u in tl["unknowns"])
    assert "squad-medic" in reasons and "remediation.external" in reasons


def test_timeline_event_by_namespace_when_incident_was_not_linked(db):
    """Событие записали ДО открытия инцидента (медик успел раньше алерта) —
    в ленте оно всё равно есть по namespace и окну."""
    record_external_remediation(db, _payload(namespaces=[]))
    inc = _incident(ns="squad-39-shared", opened=NOW + timedelta(minutes=3))
    db.add(inc)
    db.commit()

    tl = build_timeline(db, inc, now=NOW + timedelta(minutes=20))
    assert [e["kind"] for e in tl["events"] if e["kind"] == "remediation.external"] == ["remediation.external"]


def test_payload_validation_caps_lists():
    p = _payload(applied=[f"x{i}" for i in range(500)], namespaces=["a", "a", "b"])
    assert len(p.applied) == 100 and p.namespaces == ["a", "b"]
    with pytest.raises(ValueError):
        RemediationEventIn(**{**json.loads(_payload().model_dump_json()), "outcome": "weird"})
