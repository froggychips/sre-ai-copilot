"""Операционная память в timeline: Evidence → Diagnosis → Decision → Action → Verification."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, IncidentRecord
from app.knowledge_graph.incident_timeline import build_timeline
from app.knowledge_graph.incidents import attach_alert, reconcile_incidents
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent
from app.remediation.models import RemediationDecision

T0 = datetime(2026, 9, 7, 10, 0, 0)
M = timedelta(minutes=1)
FP = "fp-1"


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


def _incident(db):
    svc = upsert_service(db, namespace="squad-1", name="town-service")
    db.flush()
    db.add(AlertEvent(service_id=svc.id, alertname="PodCrashLooping", severity="critical",
                      fingerprint=FP, fired_at=T0))
    db.flush()
    return attach_alert(db, namespace="squad-1", service_name="town-service", service_id=svc.id,
                        fired_at=T0, alertname="PodCrashLooping", severity="critical", fingerprint=FP)


def _analysis(with_action=True, with_verification=True):
    a = {
        "facts": [
            {"kind": "oom_killed", "observed": True, "confidence": 0.95, "verdict": "found",
             "epistemic": "observed", "subject": "town-service-abc"},
            {"kind": "recent_deploy", "observed": False, "confidence": 0.95, "verdict": "absent"},
            {"kind": "upstream_degraded", "observed": False, "confidence": 0.0, "verdict": "unknown",
             "unknown_reason": "kg_alerts недоступен"},
        ],
        "cause": "OOMKilled: memory limit too low after traffic growth",
        "resolution_quality": "resolved",
        "fact_conflicts": [],
        "execution_intent": {"action": "restart_deployment", "resource_type": "deployment",
                             "resource_name": "town-service", "namespace": "squad-1"},
    }
    if with_action:
        a["executor_applied"] = {
            "applied_at": (T0 + 12 * M).isoformat() + "+00:00", "applied_by": "oncall",
            "result": {"success": True, "command": "kubectl rollout restart deployment/town-service -n squad-1"},
            "identity_check": "same",
            "target_before": {"uid": "u1", "generation": 5}, "target_after": {"uid": "u1", "generation": 6},
            "verification": {"scheduled": True, "attempt": 1},
        }
    if with_verification:
        a["executor_verification"] = {
            "outcome": "verified", "attempt": 1, "checked_at": (T0 + 20 * M).isoformat() + "+00:00",
            "checks": {"same_identity": True, "healthy": True}, "reasons": [],
        }
    return a


def _seed_memory(db, **kw):
    inc = _incident(db)
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={"namespace": "squad-1"},
                          analysis=_analysis(**kw), created_at=T0 + 3 * M))
    db.add(RemediationDecision(incident_id=FP, idempotency_key="k1", decision="approve",
                               selected_playbook="restart-crashloop", classification="crashloop",
                               decision_reasons=[{"rule": "risk_medium"}],
                               command_preview="kubectl rollout restart …",
                               target_ref={"uid": "u1"}, created_at=T0 + 5 * M))
    db.commit()
    return inc


def test_memory_chain_appears_in_causal_order(db):
    inc = _seed_memory(db)
    tl = build_timeline(db, inc, now=T0 + 30 * M)
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == ["incident.opened", "alert.fired", "evidence", "diagnosis", "decision",
                     "action.applied", "verification"]
    assert tl["memory"] == {
        "records": 1, "diagnosis": "OOMKilled: memory limit too low after traffic growth",
        "resolution_quality": "resolved", "decisions": 1, "actions": 1,
        "verification": "verified", "identity_check": "same", "outcome": "action_verified",
        # действия внешних исполнителей (kg_remediation_events) — в этом сценарии их нет
        "external_actions": 0,
    }
    assert tl["unknowns"] == []


def test_evidence_event_summarises_verdicts_and_carries_unknowns(db):
    inc = _seed_memory(db)
    ev = next(e for e in build_timeline(db, inc, now=T0 + 30 * M)["events"] if e["kind"] == "evidence")
    assert ev["title"] == "Свидетельства: absent 1, found 1, unknown 1"
    assert ev["details"]["found"][0]["kind"] == "oom_killed"
    assert ev["details"]["unknown"] == [{"kind": "upstream_degraded", "reason": "kg_alerts недоступен"}]
    assert ev["evidence"] == {"epistemic": "observed", "provenance": "incidents.analysis.facts"}


def test_diagnosis_is_inferred_decision_is_declared_action_is_observed(db):
    inc = _seed_memory(db)
    by = {e["kind"]: e for e in build_timeline(db, inc, now=T0 + 30 * M)["events"]}
    assert by["diagnosis"]["evidence"]["epistemic"] == "inferred"
    assert by["decision"]["evidence"] == {"epistemic": "declared", "provenance": "kg_remediation_decisions"}
    assert by["decision"]["details"]["playbook"] == "restart-crashloop"
    assert by["action.applied"]["evidence"]["epistemic"] == "observed"
    assert by["action.applied"]["details"]["identity_check"] == "same"
    assert by["action.applied"]["details"]["generation_after"] == 6
    assert by["verification"]["evidence"]["epistemic"] == "observed"
    assert by["verification"]["title"] == "Верификация: verified"


def test_pending_verification_is_unknown_epistemic_and_outcome_reflects_it(db):
    inc = _incident(db)
    a = _analysis(with_verification=True)
    a["executor_verification"].update({"outcome": "pending", "reasons": ["rollout ещё не сошёлся"]})
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={}, analysis=a, created_at=T0 + 3 * M))
    db.commit()
    tl = build_timeline(db, inc, now=T0 + 30 * M)
    ver = next(e for e in tl["events"] if e["kind"] == "verification")
    assert ver["evidence"]["epistemic"] == "unknown"
    assert ver["title"] == "Верификация: pending — rollout ещё не сошёлся"
    assert tl["memory"]["outcome"] == "action_pending"


def test_action_without_verification_is_named_unverified(db):
    inc = _incident(db)
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={},
                          analysis=_analysis(with_verification=False), created_at=T0 + 3 * M))
    db.commit()
    assert build_timeline(db, inc, now=T0 + 30 * M)["memory"]["outcome"] == "action_applied_unverified"


def test_no_analysis_is_a_known_unknown_not_silence(db):
    inc = _incident(db)
    tl = build_timeline(db, inc, now=T0 + 5 * M)
    assert [e["kind"] for e in tl["events"]] == ["incident.opened", "alert.fired"]
    assert tl["memory"]["records"] == 0 and tl["memory"]["outcome"] == "open_without_analysis"
    assert any("путь не включён" in u["reason"] for u in tl["unknowns"])


def test_resolved_without_analysis_vs_without_action(db):
    inc = _incident(db)
    db.query(AlertEvent).filter_by(fingerprint=FP).update({"resolved_at": T0 + 10 * M})
    db.flush()
    reconcile_incidents(db, now=T0 + 11 * M)
    db.refresh(inc)
    assert build_timeline(db, inc, now=T0 + 30 * M)["memory"]["outcome"] == "resolved_without_analysis"
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={},
                          analysis={"cause": "flap", "resolution_quality": "resolved"}, created_at=T0 + 3 * M))
    db.commit()
    assert build_timeline(db, inc, now=T0 + 30 * M)["memory"]["outcome"] == "resolved_without_action"


def test_diagnosis_not_made_uses_triage_note(db):
    inc = _incident(db)
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={},
                          analysis={"cause": None, "triage_note": "No hypothesis survived",
                                    "resolution_quality": "unresolved"}, created_at=T0 + 3 * M))
    db.commit()
    diag = next(e for e in build_timeline(db, inc, now=T0 + 30 * M)["events"] if e["kind"] == "diagnosis")
    assert diag["title"] == "Диагноз не поставлен: No hypothesis survived"


# ── kg_remediation_attempts как источник истины ─────────────────────────────


def _attempt_row(status, *, intent=None, result=None, verification=None):
    from app.remediation.attempts import RemediationAttempt

    applied = _analysis()["executor_applied"]
    return RemediationAttempt(
        incident_id=FP, signature=f"sig-{status}", status=status,
        intent=intent or {"action": "restart_deployment", "resource_type": "deployment",
                          "resource_name": "town-service", "namespace": "squad-1"},
        applied_by="oncall", claimed_at=T0 + 11 * M,
        applied_at=T0 + 12 * M if status != "unknown" else None,
        result=result if result is not None else (applied if status != "unknown" else None),
        verification=verification, error="stale_claim" if status == "unknown" else None,
        created_at=T0 + 11 * M, updated_at=T0 + 21 * M,
    )


def test_attempt_row_wins_over_json_and_carries_applied_intent(db):
    """Re-fire перезаписал analysis.execution_intent новым планом: действие в
    хронологии — то, что реально применили (intent строки), а не новый план."""
    inc = _incident(db)
    a = _analysis(with_action=False, with_verification=False)
    a["execution_intent"] = {"action": "scale_deployment", "resource_type": "deployment",
                             "resource_name": "other-service", "namespace": "squad-1"}
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={}, analysis=a,
                          created_at=T0 + 3 * M))
    db.add(_attempt_row("verified", verification={
        "outcome": "verified", "attempt": 2, "checked_at": (T0 + 20 * M).isoformat(),
        "checks": {}, "reasons": []}))
    db.commit()
    tl = build_timeline(db, inc, now=T0 + 30 * M)
    by = {e["kind"]: e for e in tl["events"]}
    assert by["action.applied"]["details"]["resource"] == "town-service"
    assert by["action.applied"]["evidence"]["provenance"].startswith("kg_remediation_attempts#")
    assert by["verification"]["details"]["attempt"] == 2
    assert tl["memory"]["outcome"] == "action_verified"


def test_row_present_json_leftovers_are_not_double_counted(db):
    """Dual-write: строка и JSON про одно действие — событие одно."""
    inc = _incident(db)
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={},
                          analysis=_analysis(), created_at=T0 + 3 * M))
    db.add(_attempt_row("applied"))
    db.commit()
    tl = build_timeline(db, inc, now=T0 + 30 * M)
    assert [e["kind"] for e in tl["events"]].count("action.applied") == 1
    # верификации в строке ещё нет — JSON-хвост не подмешивается
    assert tl["memory"]["outcome"] == "action_applied_unverified"


def test_unknown_attempt_is_visible_and_not_counted_as_applied(db):
    inc = _incident(db)
    db.add(IncidentRecord(incident_id=FP, status="COMPLETED", data={},
                          analysis=_analysis(with_action=False, with_verification=False),
                          created_at=T0 + 3 * M))
    db.add(_attempt_row("unknown"))
    db.commit()
    tl = build_timeline(db, inc, now=T0 + 30 * M)
    ev = next(e for e in tl["events"] if e["kind"] == "action.state_unknown")
    assert ev["evidence"]["epistemic"] == "unknown"
    assert ev["details"]["error"] == "stale_claim"
    assert tl["memory"]["actions"] == 0
    assert tl["memory"]["outcome"] == "action_state_unknown"
