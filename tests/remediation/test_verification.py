"""Верификация remediation: та же ли цель, сработало ли, стало ли лучше."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.execution_dsl import ExecutionIntent
from app.database import Base, IncidentRecord
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent, PodEvent
from app.remediation import verification as v
from app.remediation.models import RemediationDecision
from app.services import executor_apply
from app.services.incident_state import EXECUTOR_VERIFIED, EXECUTOR_VERIFY_FAILED

NS, NAME = "squad-1", "town-service"


def _intent(action="restart_deployment", **params) -> ExecutionIntent:
    return ExecutionIntent(action=action, resource_type="deployment", resource_name=NAME,
                           namespace=NS, params=params, risk="low")


def _deploy_json(uid="uid-1", generation=5, observed=5, desired=3, ready=3, image="svc:1"):
    return {
        "metadata": {"name": NAME, "namespace": NS, "uid": uid, "generation": generation,
                     "resourceVersion": "100"},
        "spec": {"replicas": desired, "template": {"spec": {"containers": [{"image": image}]}}},
        "status": {"observedGeneration": observed, "readyReplicas": ready},
    }


def _runner(obj=None, *, returncode=0, stderr="", raise_exc=None):
    def run(argv, **kw):
        if raise_exc:
            raise raise_exc
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(obj or {}), stderr=stderr)
    return run


# ── снимок ────────────────────────────────────────────────────────────────

def test_snapshot_parses_deployment_identity_and_template_hash():
    snap = v.snapshot_target(_intent(), runner=_runner(_deploy_json()))
    assert not snap.unknown
    assert snap.uid == "uid-1" and snap.generation == 5 and snap.observed_generation == 5
    assert snap.replicas_desired == 3 and snap.replicas_ready == 3
    assert snap.template_hash and len(snap.template_hash) == 16
    assert snap.converged is True and snap.healthy is True


def test_snapshot_template_hash_ignores_replicas_but_sees_image():
    a = v.snapshot_target(_intent(), runner=_runner(_deploy_json(desired=3)))
    b = v.snapshot_target(_intent(), runner=_runner(_deploy_json(desired=9)))
    c = v.snapshot_target(_intent(), runner=_runner(_deploy_json(image="svc:2")))
    assert a.template_hash == b.template_hash != c.template_hash


def test_snapshot_uses_read_only_kubectl_get():
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=json.dumps(_deploy_json()), stderr="")
    v.snapshot_target(_intent(), runner=run)
    assert seen["argv"] == ["kubectl", "get", "deployment", NAME, "-n", NS, "-o", "json"]


@pytest.mark.parametrize("runner,reason", [
    (_runner(raise_exc=RuntimeError("breaker open")), "kubectl_failed:RuntimeError"),
    (_runner(returncode=1, stderr='Error from server (NotFound): deployments "x" not found'), "target_not_found"),
    (lambda argv, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr=""), "kubectl_output_not_json"),
    (lambda argv, **kw: SimpleNamespace(returncode=0, stdout="{}", stderr=""), "kubectl_output_empty"),
])
def test_snapshot_is_unknown_not_exception_when_kubectl_fails(runner, reason):
    snap = v.snapshot_target(_intent(), runner=runner)
    assert snap.unknown and snap.reason == reason
    assert snap.converged is None and snap.healthy is None


# ── идентичность ──────────────────────────────────────────────────────────

def test_identity_mismatch_refuses_reincarnated_target():
    live = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-NEW")))
    assert "пересоздан" in (v.identity_mismatch({"uid": "uid-OLD"}, live) or "")
    assert v.identity_check({"uid": "uid-OLD"}, live) == "mismatch"


def test_identity_same_and_unknown_cases():
    live = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-1")))
    assert v.identity_mismatch({"uid": "uid-1"}, live) is None
    assert v.identity_check({"uid": "uid-1"}, live) == "same"
    assert v.identity_mismatch(None, live) is None                       # сверять нечем
    assert v.identity_check(None, live) == "unknown:no_expected_uid"
    dead = v.TargetSnapshot.unavailable("kubectl_failed", _intent())
    assert v.identity_mismatch({"uid": "uid-1"}, dead) is None            # нет снимка — не отказ
    assert v.identity_check({"uid": "uid-1"}, dead) == "unknown:no_live_snapshot"


def test_expected_identity_reads_latest_decision():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(RemediationDecision(incident_id="inc-1", idempotency_key="a", target_ref={"uid": "old"},
                               created_at=datetime(2026, 9, 6, 9, 0)))
    db.add(RemediationDecision(incident_id="inc-1", idempotency_key="b", target_ref={"uid": "new", "incarnation": 2},
                               created_at=datetime(2026, 9, 6, 10, 0)))
    db.commit()
    assert v.expected_identity(db, "inc-1") == {"uid": "new", "incarnation": 2, "kind": None,
                                                "namespace": None, "name": None}
    assert v.expected_identity(db, "inc-none") is None
    assert v.expected_identity(MagicMock(), "inc-1") is None            # MagicMock.target_ref — не dict


# ── apply_intent: отказ при пересозданной цели, снимки в записи ──────────

def _apply_env(monkeypatch, *, expected, live_before, live_after=None):
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.with_for_update.return_value = query
    monkeypatch.setattr(executor_apply, "SessionLocal", lambda: session)
    snaps = iter([live_before, live_after or live_before])
    monkeypatch.setattr(executor_apply, "snapshot_target", lambda intent, **kw: next(snaps))
    monkeypatch.setattr(executor_apply, "expected_identity", lambda db, incident_id: expected)
    scheduled = {}
    monkeypatch.setattr(executor_apply, "schedule_verification",
                        lambda incident_id, **kw: scheduled.setdefault("call", {"scheduled": True, "attempt": 1}))
    intent = {"action": "restart_deployment", "resource_type": "deployment",
              "resource_name": NAME, "namespace": NS, "params": {}, "risk": "low"}
    record = MagicMock()
    record.analysis = {"execution_intent": intent, "executor_result": {"status": "dry_run_ok"}}
    record.data = {"namespace": NS}
    record.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    query.first.return_value = record
    from app.services.intent_signature import compute_signature
    sig = compute_signature(ExecutionIntent.model_validate(intent))
    approval = SimpleNamespace(status="approved", decided_at=datetime.now(timezone.utc).replace(tzinfo=None))
    monkeypatch.setattr(executor_apply, "_load_approval", lambda db, i, s: approval)
    def fake_exec(intent, dry_run=True, post_approval=False, **kw):
        return {"success": True, "command": "kubectl …", "exit_code": 0}
    monkeypatch.setattr(executor_apply.k8s_service, "execute_intent", fake_exec)
    return record, sig, scheduled


def test_apply_refuses_when_target_uid_differs_from_graph(monkeypatch):
    live = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-NEW")))
    record, sig, scheduled = _apply_env(monkeypatch, expected={"uid": "uid-OLD"}, live_before=live)
    out = executor_apply.apply_intent("inc-1", "user", sig)
    assert out["ok"] is False and out["reason"].startswith("target_reincarnated:")
    assert "executor_applied" not in record.analysis        # kubectl write не было
    assert not scheduled


def test_apply_records_snapshots_identity_and_schedules_verification(monkeypatch):
    before = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-1", generation=5)))
    after = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-1", generation=6, observed=5,
                                                                      image="svc:1-restarted")))
    record, sig, scheduled = _apply_env(monkeypatch, expected={"uid": "uid-1"}, live_before=before, live_after=after)
    out = executor_apply.apply_intent("inc-1", "user", sig)
    assert out["ok"] is True
    applied = record.analysis["executor_applied"]
    assert applied["identity_check"] == "same"
    assert applied["target_before"]["generation"] == 5 and applied["target_after"]["generation"] == 6
    assert applied["target_before"]["template_hash"] != applied["target_after"]["template_hash"]
    assert applied["verification"] == {"scheduled": True, "attempt": 1}
    assert scheduled


def test_apply_proceeds_with_unknown_identity_when_graph_has_no_uid(monkeypatch):
    live = v.snapshot_target(_intent(), runner=_runner(_deploy_json(uid="uid-1")))
    record, sig, _ = _apply_env(monkeypatch, expected=None, live_before=live)
    assert executor_apply.apply_intent("inc-1", "user", sig)["ok"] is True
    assert record.analysis["executor_applied"]["identity_check"] == "unknown:no_expected_uid"


# ── оценка исхода (чистая функция) ──────────────────────────────────────

def _snap(**kw):
    return v.snapshot_target(_intent(), runner=_runner(_deploy_json(**kw)))


def _before(**kw):
    return _snap(**kw).to_dict()


def test_assess_verified_when_restart_applied_converged_healthy_and_alert_resolved():
    r = v.assess(intent=_intent(), before=_before(generation=5), now_snap=_snap(generation=6, observed=6, image="svc:2"),
                 alert_resolved=True, new_crash_events=0, attempt=1, max_attempts=2)
    assert r["outcome"] == "verified"
    assert r["checks"] == {"same_identity": True, "action_took_effect": True, "converged": True,
                           "healthy": True, "alert_resolved": True, "new_crash_events": 0}


def test_assess_pending_while_rollout_not_converged_then_failed_on_last_attempt():
    now = _snap(generation=6, observed=5, ready=1, image="svc:2")
    first = v.assess(intent=_intent(), before=_before(), now_snap=now, alert_resolved=False,
                     new_crash_events=0, attempt=1, max_attempts=2)
    last = v.assess(intent=_intent(), before=_before(), now_snap=now, alert_resolved=False,
                    new_crash_events=0, attempt=2, max_attempts=2)
    assert first["outcome"] == "pending" and last["outcome"] == "failed"
    assert any("не сошёлся" in x for x in first["reasons"])


def test_assess_failed_on_new_crash_events_even_if_healthy():
    r = v.assess(intent=_intent(), before=_before(), now_snap=_snap(generation=6, observed=6, image="svc:2"),
                 alert_resolved=True, new_crash_events=3, attempt=1, max_attempts=2)
    assert r["outcome"] == "failed" and any("CrashLoop/OOM" in x for x in r["reasons"])


def test_assess_failed_when_restart_did_not_change_template():
    r = v.assess(intent=_intent(), before=_before(generation=5), now_snap=_snap(generation=5, observed=5),
                 alert_resolved=True, new_crash_events=0, attempt=1, max_attempts=2)
    assert r["outcome"] == "failed" and r["checks"]["action_took_effect"] is False


def test_assess_unknown_when_uid_changed_after_action_or_no_snapshot():
    changed = v.assess(intent=_intent(), before=_before(uid="uid-1"), now_snap=_snap(uid="uid-2"),
                       alert_resolved=True, new_crash_events=0, attempt=1, max_attempts=2)
    assert changed["outcome"] == "unknown" and changed["checks"]["same_identity"] is False
    gone = v.assess(intent=_intent(), before=_before(), now_snap=v.TargetSnapshot.unavailable("kubectl_failed"),
                    alert_resolved=None, new_crash_events=0, attempt=1, max_attempts=2)
    assert gone["outcome"] == "unknown" and gone["checks"] == {"snapshot": None}


def test_assess_scale_checks_replicas_and_alert_unknown_is_not_failure():
    r = v.assess(intent=_intent("scale_deployment", replicas=5), before=_before(desired=3),
                 now_snap=_snap(desired=5, ready=5), alert_resolved=None, new_crash_events=0,
                 attempt=1, max_attempts=2)
    assert r["outcome"] == "verified" and r["checks"]["action_took_effect"] is True
    assert any("kg_alerts" in x for x in r["reasons"])


def test_assess_pending_while_alert_still_firing():
    r = v.assess(intent=_intent(), before=_before(), now_snap=_snap(generation=6, observed=6, image="svc:2"),
                 alert_resolved=False, new_crash_events=0, attempt=1, max_attempts=2)
    assert r["outcome"] == "pending" and "алерт всё ещё firing" in r["reasons"]


# ── verify_remediation: запись в инцидент и состояние ────────────────────

@pytest.fixture
def kg_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _seed_incident(db, before: dict, fingerprint="inc-1", alert_resolved=True, crash_after=0):
    applied_at = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    rec = IncidentRecord(
        incident_id=fingerprint, status="COMPLETED",
        data={"namespace": NS},
        analysis={
            "execution_intent": {"action": "restart_deployment", "resource_type": "deployment",
                                 "resource_name": NAME, "namespace": NS, "params": {}, "risk": "low"},
            "executor_applied": {"applied_at": applied_at.isoformat(), "target_before": before},
        },
    )
    db.add(rec)
    svc = upsert_service(db, namespace=NS, name=NAME)
    db.flush()
    db.add(AlertEvent(service_id=svc.id, alertname="PodCrashLooping", severity="critical",
                      fingerprint=fingerprint, fired_at=applied_at.replace(tzinfo=None) - timedelta(minutes=30),
                      resolved_at=(applied_at.replace(tzinfo=None) + timedelta(minutes=3)) if alert_resolved else None))
    for i in range(crash_after):
        db.add(PodEvent(service_id=svc.id, namespace=NS, pod_name=f"{NAME}-x{i}", reason="BackOff",
                        message="m", type="Warning", event_uid=f"e{i}",
                        first_seen=applied_at.replace(tzinfo=None) + timedelta(minutes=2 + i),
                        last_seen=applied_at.replace(tzinfo=None) + timedelta(minutes=3 + i), count=1))
    db.commit()
    return rec


def test_verify_remediation_marks_verified_and_records_checks(kg_db, monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    db = kg_db()
    _seed_incident(db, _before(generation=5))
    db.close()
    with patch("app.services.audit_logger.audit_service.log_event") as audit:
        out = v.verify_remediation("inc-1", 1, db_factory=kg_db,
                                   runner=_runner(_deploy_json(generation=6, observed=6, image="svc:2")))
    assert out["outcome"] == "verified" and out["next_delay_sec"] is None
    rec = kg_db().query(IncidentRecord).filter_by(incident_id="inc-1").one()
    ver = rec.analysis["executor_verification"]
    assert ver["outcome"] == "verified" and ver["checks"]["alert_resolved"] is True
    assert rec.analysis["executor_verification_history"][-1]["outcome"] == "verified"
    assert rec.executor_state == EXECUTOR_VERIFIED
    assert audit.call_args.args[0] == "EXECUTOR_VERIFY_VERIFIED"


def test_verify_remediation_failed_on_crash_events_sets_state(kg_db, monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    db = kg_db()
    _seed_incident(db, _before(generation=5), crash_after=2)
    db.close()
    with patch("app.services.audit_logger.audit_service.log_event"):
        out = v.verify_remediation("inc-1", 1, db_factory=kg_db,
                                   runner=_runner(_deploy_json(generation=6, observed=6, image="svc:2")))
    assert out["outcome"] == "failed" and out["checks"]["new_crash_events"] == 2
    assert kg_db().query(IncidentRecord).filter_by(incident_id="inc-1").one().executor_state == EXECUTOR_VERIFY_FAILED


def test_verify_remediation_pending_schedules_next_attempt(kg_db, monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    db = kg_db()
    _seed_incident(db, _before(generation=5), alert_resolved=False)
    db.close()
    with patch("app.services.audit_logger.audit_service.log_event"):
        out = v.verify_remediation("inc-1", 1, db_factory=kg_db,
                                   runner=_runner(_deploy_json(generation=6, observed=6, image="svc:2")))
    assert out["outcome"] == "pending" and out["next_delay_sec"] == 900
    rec = kg_db().query(IncidentRecord).filter_by(incident_id="inc-1").one()
    assert rec.executor_state is None          # pending не меняет состояние


def test_verify_remediation_unknown_when_nothing_applied(kg_db):
    db = kg_db()
    db.add(IncidentRecord(incident_id="inc-9", status="COMPLETED", data={}, analysis={}))
    db.commit()
    db.close()
    assert v.verify_remediation("inc-9", 1, db_factory=kg_db, runner=_runner({}))["reason"] == "nothing_applied"
    assert v.verify_remediation("inc-none", 1, db_factory=kg_db, runner=_runner({}))["reason"] == "incident_not_found"


# ── планирование ─────────────────────────────────────────────────────────

def test_verify_delays_parse_and_fallback(monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "60, 120,abc", raising=False)
    assert v.verify_delays() == [60, 120]
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "", raising=False)
    assert v.verify_delays() == [300, 900]


def test_schedule_verification_disabled_or_broker_down(monkeypatch):
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_ENABLED", False, raising=False)
    assert v.schedule_verification("inc-1")["reason"] == "disabled"
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_ENABLED", True, raising=False)
    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    with patch("app.workers.tasks.remediation_verify_task") as task:
        task.apply_async.side_effect = ConnectionError("redis down")
        out = v.schedule_verification("inc-1")
    assert out["scheduled"] is False and out["reason"].startswith("broker:")
    with patch("app.workers.tasks.remediation_verify_task") as task:
        out = v.schedule_verification("inc-1", attempt=1)
        task.apply_async.assert_called_once_with(args=["inc-1", 1], countdown=300)
    assert out == {"scheduled": True, "attempt": 1, "delay_sec": 300}
    assert v.schedule_verification("inc-1", attempt=3)["reason"] == "no_more_attempts"
