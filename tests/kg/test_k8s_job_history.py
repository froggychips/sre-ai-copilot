"""История состояний Job-ов (kg_k8s_job_runs) и запрос «на момент T».

Сценарий из жизни: migrate-job сквада упал с BackoffLimitExceeded, через
сутки его перезапустили с тем же именем и он прошёл. В kg_k8s_jobs остался
только успех; разбор инцидента, случившегося между ними, должен видеть падение.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.k8s_job_history import (jobs_state_at, latest_run_states,
                                                 prune_job_runs, record_job_run,
                                                 terminal_condition)
from app.knowledge_graph.k8s_jobs_sync import sync_all_jobs
from app.knowledge_graph.schema import K8sJob, K8sJobRun

T0 = datetime(2026, 9, 12, 19, 0, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(name, ns, *, uid="u1", succeeded=0, failed=0, active=0, cond=None, reason=None,
         message=None):
    status = {"succeeded": succeeded, "failed": failed, "active": active}
    if cond:
        status["conditions"] = [{"type": cond, "status": "True", "reason": reason,
                                 "message": message}]
    return {
        "metadata": {"name": name, "namespace": ns, "uid": uid, "labels": {}},
        "spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": [{}]}}},
        "status": status,
    }


def _sync(db, jobs, now):
    with patch("app.knowledge_graph.k8s_jobs_sync._kubectl_get_all", return_value=jobs), \
         patch("app.knowledge_graph.k8s_jobs_sync._kubectl_get_pod_exit_code", return_value=1), \
         patch("app.knowledge_graph.k8s_job_history.datetime") as dt:
        dt.utcnow.return_value = now
        return sync_all_jobs(db)


# ── terminal_condition ──────────────────────────────────────────────────────


def test_terminal_condition_prefers_failed_and_truncates_message():
    job = _job("m", "ns", cond="Failed", reason="BackoffLimitExceeded", message="x" * 900)
    job["status"]["conditions"].append({"type": "Complete", "status": "True"})
    t, r, m = terminal_condition(job)
    assert (t, r) == ("Failed", "BackoffLimitExceeded")
    assert len(m) <= 501


def test_terminal_condition_ignores_false_status():
    job = _job("m", "ns")
    job["status"]["conditions"] = [{"type": "Failed", "status": "False"}]
    assert terminal_condition(job) == (None, None, None)


# ── запись на изменение ─────────────────────────────────────────────────────


def test_sync_records_only_on_state_change(db):
    running = _job("config-db-migrate", "squad-19-shared", active=1)
    s1 = _sync(db, [running], T0)
    s2 = _sync(db, [running], T0 + timedelta(minutes=15))
    assert s1["job_runs_recorded"] == 1
    assert s2["job_runs_recorded"] == 0, "стабильный тик не пишет историю"

    failed = _job("config-db-migrate", "squad-19-shared", failed=2,
                  cond="Failed", reason="BackoffLimitExceeded")
    s3 = _sync(db, [failed], T0 + timedelta(minutes=30))
    assert s3["job_runs_recorded"] == 1
    rows = db.query(K8sJobRun).order_by(K8sJobRun.id).all()
    assert [r.condition_reason for r in rows] == [None, "BackoffLimitExceeded"]
    assert rows[1].last_pod_exit_code == 1
    # снимок по-прежнему один
    assert db.query(K8sJob).count() == 1


def test_rerun_with_same_name_new_uid_is_new_row(db):
    failed = _job("map-db-migrate", "squad-19-kingdom5", uid="u1", failed=2,
                  cond="Failed", reason="BackoffLimitExceeded")
    _sync(db, [failed], T0)
    ok = _job("map-db-migrate", "squad-19-kingdom5", uid="u2", succeeded=1, cond="Complete")
    _sync(db, [ok], T0 + timedelta(days=1))
    rows = db.query(K8sJobRun).order_by(K8sJobRun.id).all()
    assert [r.uid for r in rows] == ["u1", "u2"]
    # снимок забыл падение — история помнит
    assert db.query(K8sJob).one().failed_count == 0


def test_history_unavailable_keeps_snapshot(db):
    with patch("app.knowledge_graph.k8s_jobs_sync.latest_run_states",
               side_effect=RuntimeError("no table")):
        stats = _sync(db, [_job("j", "ns", active=1)], T0)
    assert stats["job_runs_unavailable"] == 1
    assert stats["job_runs_recorded"] == 0
    assert db.query(K8sJob).count() == 1
    assert db.query(K8sJobRun).count() == 0


def test_record_job_run_updates_prev_in_place(db):
    prev = {}
    f = {"uid": "u", "active_count": 1}
    assert record_job_run(db, namespace="ns", name="j", fields=f, prev=prev, now=T0)
    assert not record_job_run(db, namespace="ns", name="j", fields=f, prev=prev, now=T0)
    db.flush()
    assert latest_run_states(db) == prev


# ── jobs_state_at ───────────────────────────────────────────────────────────


def test_state_at_sees_failure_that_snapshot_lost(db):
    """Инцидент между падением и успешным перезапуском видит падение."""
    _sync(db, [_job("config-db-migrate", "squad-19-shared", uid="u1", failed=2,
                    cond="Failed", reason="BackoffLimitExceeded")], T0)
    _sync(db, [_job("config-db-migrate", "squad-19-shared", uid="u2", succeeded=1,
                    cond="Complete")], T0 + timedelta(days=2))
    db.commit()

    at_incident = jobs_state_at(db, ["squad-19-shared"], T0 + timedelta(hours=6))
    assert [(j["name"], j["status"], j["condition_reason"]) for j in at_incident] == [
        ("config-db-migrate", "failed", "BackoffLimitExceeded"),
    ]
    later = jobs_state_at(db, ["squad-19-shared"], T0 + timedelta(days=2, hours=1))
    assert later[0]["status"] == "succeeded"


def test_state_at_ignores_future_and_old_success(db):
    _sync(db, [_job("old-ok", "ns", succeeded=1, cond="Complete")], T0 - timedelta(days=5))
    _sync(db, [_job("future", "ns", failed=1, cond="Failed")], T0 + timedelta(hours=1))
    db.commit()
    assert jobs_state_at(db, ["ns"], T0) == []


def test_state_at_keeps_lingering_failure_and_orders_failed_first(db):
    _sync(db, [_job("mig", "ns", failed=3, cond="Failed", reason="BackoffLimitExceeded")],
          T0 - timedelta(days=2))
    _sync(db, [_job("mig", "ns", failed=3, cond="Failed", reason="BackoffLimitExceeded"),
               _job("seed", "ns", succeeded=1, cond="Complete")], T0 - timedelta(hours=1))
    db.commit()
    names = [(j["name"], j["status"]) for j in jobs_state_at(db, ["ns"], T0)]
    assert names == [("mig", "failed"), ("seed", "succeeded")]


def test_state_at_name_filter_and_empty_namespaces(db):
    _sync(db, [_job("chat-db-migrate", "ns", active=1), _job("backup", "ns", active=1)], T0)
    db.commit()
    assert [j["name"] for j in jobs_state_at(db, ["ns"], T0, name_contains="MIGRAT")] == [
        "chat-db-migrate",
    ]
    assert jobs_state_at(db, [], T0) == []


# ── retention ───────────────────────────────────────────────────────────────


def test_prune_keeps_last_row_per_job(db):
    prev = {}
    old = T0 - timedelta(days=40)
    record_job_run(db, namespace="ns", name="j", fields={"uid": "a", "active_count": 1},
                   prev=prev, now=old)
    record_job_run(db, namespace="ns", name="j", fields={"uid": "a", "failed_count": 1},
                   prev=prev, now=old + timedelta(hours=1))
    record_job_run(db, namespace="ns", name="k", fields={"uid": "b", "active_count": 1},
                   prev=prev, now=T0)
    db.flush()
    assert prune_job_runs(db, retention_days=30, now=T0) == 1
    left = sorted((r.name, r.failed_count) for r in db.query(K8sJobRun).all())
    assert left == [("j", 1), ("k", None)]


# ── ревью: пропавшие Job-ы и инкарнации namespace-а ─────────────────────────


def test_deleted_running_job_is_not_running_forever(db):
    _sync(db, [_job("mig", "ns", active=1), _job("keep", "ns", active=1)], T0)
    stats = _sync(db, [_job("keep", "ns", active=1)], T0 + timedelta(minutes=15))
    assert stats["job_runs_disappeared"] == 1
    db.commit()
    names = [j["name"] for j in jobs_state_at(db, ["ns"], T0 + timedelta(days=3))]
    assert names == ["keep"], "удалённый running-Job не должен жить вечно"
    before = {j["name"] for j in jobs_state_at(db, ["ns"], T0 + timedelta(minutes=5))}
    assert before == {"mig", "keep"}


def test_deleted_failed_job_stays_failed(db):
    _sync(db, [_job("mig", "ns", failed=3, cond="Failed", reason="BackoffLimitExceeded"),
               _job("keep", "ns", active=1)], T0)
    _sync(db, [_job("keep", "ns", active=1)], T0 + timedelta(hours=1))
    db.commit()
    got = {j["name"]: j for j in jobs_state_at(db, ["ns"], T0 + timedelta(hours=2))}
    assert got["mig"]["status"] == "failed" and got["mig"]["disappeared"]


def test_empty_fetch_writes_no_tombstones(db):
    _sync(db, [_job("mig", "ns", active=1)], T0)
    stats = _sync(db, [], T0 + timedelta(minutes=15))
    assert stats["job_runs_disappeared"] == 0
    assert db.query(K8sJobRun).count() == 1


def test_reappeared_job_after_tombstone_is_new_row(db):
    _sync(db, [_job("mig", "ns", uid="a", active=1), _job("k", "ns", active=1)], T0)
    _sync(db, [_job("k", "ns", active=1)], T0 + timedelta(minutes=15))
    _sync(db, [_job("mig", "ns", uid="a", active=1), _job("k", "ns", active=1)],
          T0 + timedelta(minutes=30))
    db.commit()
    rows = db.query(K8sJobRun).filter(K8sJobRun.name == "mig").order_by(K8sJobRun.id).all()
    assert [r.disappeared for r in rows] == [False, True, False]


def test_previous_namespace_incarnation_is_ignored(db):
    from app.knowledge_graph.schema import Namespace
    _sync(db, [_job("mig", "squad-5-shared", failed=2, cond="Failed")], T0)
    # стенд снесли и подняли заново под тем же именем
    db.add(Namespace(namespace="squad-5-shared", state="active",
                     k8s_created_at=T0 + timedelta(hours=2)))
    db.commit()
    assert jobs_state_at(db, ["squad-5-shared"], T0 + timedelta(hours=5)) == []
    # вопрос про момент прошлой инкарнации — история на месте
    past = jobs_state_at(db, ["squad-5-shared"], T0 + timedelta(hours=1))
    assert past[0]["status"] == "failed"
