"""Regression tests for two review findings.

FIX A (#4 — concurrent re-fire double-dispatch): the re-fire (flapping /
FAILED-retry) UPDATE path in `/webhooks/alertmanager` had no unique-violation
guard, so two concurrent webhook requests both flipped the row RESOLVED→OPEN
and BOTH dispatched the pipeline (double LLM burn + duplicate @here). The fix
makes the claim an atomic compare-and-swap (`_claim_refire`): exactly one
caller wins (rowcount 1 → dispatch), the loser gets rowcount 0 → deduplicated.

FIX B (#3 — Celery autoretry self-defeating): the worker set the incident to
FAILED (terminal) BEFORE re-raising for Celery autoretry, so the retry died on
an invalid FAILED→INVESTIGATING transition and wasted an Analyzer LLM call.
The fix records FAILED only on a *terminal* failure — a non-retriable
exception, or the last retry attempt.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, MagicMock

from app.core.state_machine import IncidentState
from app.database import Base, IncidentRecord


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture
def db():
    """In-memory SQLite session with the incidents table."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def shared_engine():
    """Shared in-memory engine (StaticPool) — survives multiple sessions.

    async_process_incident opens/closes its OWN SessionLocal; StaticPool keeps
    the same underlying connection so a separate verification session sees the
    committed rows.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        yield engine, Session
    finally:
        engine.dispose()


def _webhook_payload(fingerprint: str, *, status: str = "firing"):
    from app.models.incident import AlertManagerWebhook

    return AlertManagerWebhook(
        version="4",
        groupKey=f"grp-{fingerprint}",
        status=status,
        alerts=[
            {
                "status": status,
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                },
                "annotations": {"summary": "s", "description": "d"},
                "startsAt": "2026-07-02T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prom.local",
                "fingerprint": fingerprint,
            }
        ],
    )


# ──────────────────────────────────────────────────────────────────────────
# FIX A — atomic re-fire claim (compare-and-swap)
# ──────────────────────────────────────────────────────────────────────────
def _decision(rows: int) -> str:
    """Map _claim_refire rowcount → what the endpoint does with it."""
    return "dispatch" if rows == 1 else "deduplicated"


def test_two_concurrent_refire_claims_exactly_one_dispatches(db):
    """Both workers observe RESOLVED and race to claim — only one dispatches."""
    from app.api.webhooks import _claim_refire

    db.add(IncidentRecord(
        incident_id="FP-A",
        status=IncidentState.RESOLVED.value,
        data={"flap_count": 0},
    ))
    db.commit()

    # Two concurrent re-fire claims against the same row, both having read
    # status=RESOLVED before either wrote.
    d1 = _decision(_claim_refire(db, "FP-A", IncidentState.RESOLVED.value,
                                 {"flap_count": 1}))
    d2 = _decision(_claim_refire(db, "FP-A", IncidentState.RESOLVED.value,
                                 {"flap_count": 1}))

    assert sorted([d1, d2]) == ["deduplicated", "dispatch"]

    row = db.query(IncidentRecord).filter_by(incident_id="FP-A").first()
    assert row.status == IncidentState.OPEN.value      # claimed → OPEN
    assert row.data["flap_count"] == 1                 # flap_count persisted


def test_claim_refire_returns_zero_once_row_is_open(db):
    """Explicit: conditional UPDATE matches 0 rows when status is already OPEN."""
    from app.api.webhooks import _claim_refire

    db.add(IncidentRecord(
        incident_id="FP-B",
        status=IncidentState.FAILED.value,
        data={},
    ))
    db.commit()

    assert _claim_refire(db, "FP-B", IncidentState.FAILED.value, {}) == 1
    # Row is OPEN now — a second claim expecting the old FAILED matches nothing.
    assert _claim_refire(db, "FP-B", IncidentState.FAILED.value, {}) == 0


def test_refire_endpoint_dispatches_once_and_persists_flap_count(db, monkeypatch):
    """End-to-end: a flapping re-fire dispatches exactly one pipeline task and
    persists the incremented flap_count + TeamCity enrichment in the row."""
    import app.api.webhooks as webhooks

    db.add(IncidentRecord(
        incident_id="FP-C",
        status=IncidentState.RESOLVED.value,
        data={"flap_count": 2},
    ))
    db.commit()

    monkeypatch.setattr(webhooks, "incident_teamcity_context",
                        AsyncMock(return_value={"deploy": "build-42"}))
    monkeypatch.setattr(webhooks.raw_collector, "ingest", MagicMock())
    monkeypatch.setattr(webhooks.settings, "PIPELINE_DIRECT_INVOKE", False)
    fake_task = MagicMock()
    fake_task.id = "task-xyz"
    delay = MagicMock(return_value=fake_task)
    monkeypatch.setattr(webhooks.process_incident_task, "delay", delay)

    result = asyncio.run(webhooks.alertmanager_webhook(_webhook_payload("FP-C"), db=db))

    alert = result["alerts"][0]
    assert alert["task_id"] == "task-xyz"          # dispatched (not deduplicated)
    assert delay.call_count == 1                    # exactly one dispatch

    row = db.query(IncidentRecord).filter_by(incident_id="FP-C").first()
    assert row.status == IncidentState.OPEN.value
    assert row.data["flap_count"] == 3             # 2 → 3, persisted
    assert row.data["teamcity_context"] == {"deploy": "build-42"}


def test_refire_endpoint_second_immediate_fire_is_deduplicated(db, monkeypatch):
    """Once the re-fire is claimed (OPEN = pipeline in-flight), a further firing
    webhook for the same fingerprint is deduplicated — no second dispatch."""
    import app.api.webhooks as webhooks

    db.add(IncidentRecord(
        incident_id="FP-D",
        status=IncidentState.OPEN.value,   # already in-flight
        data={"flap_count": 1},
    ))
    db.commit()

    monkeypatch.setattr(webhooks, "incident_teamcity_context",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(webhooks.raw_collector, "ingest", MagicMock())
    delay = MagicMock()
    monkeypatch.setattr(webhooks.process_incident_task, "delay", delay)

    result = asyncio.run(webhooks.alertmanager_webhook(_webhook_payload("FP-D"), db=db))

    assert result["alerts"][0]["task_id"] == "deduplicated"
    assert delay.call_count == 0


# ──────────────────────────────────────────────────────────────────────────
# FIX B — FAILED written only on terminal failure
# ──────────────────────────────────────────────────────────────────────────
def _run_with_pipeline_error(shared_engine, monkeypatch, *, exc, retries, max_retries):
    """Pre-insert an OPEN incident, make pipeline.run() raise `exc`, invoke
    async_process_incident with the given retry context; return the reloaded row.
    """
    engine, Session = shared_engine
    import app.workers.tasks as tasks_mod

    seed = Session()
    seed.add(IncidentRecord(
        incident_id="RETRY-1",
        status=IncidentState.OPEN.value,
        data={},
        analysis=None,
    ))
    seed.commit()
    seed.close()

    monkeypatch.setattr(tasks_mod, "SessionLocal", Session)
    monkeypatch.setattr(tasks_mod.audit_service, "log_event", lambda *a, **k: None)

    class _FakePipeline:
        def __init__(self, *a, **k):
            pass

        async def run(self):
            raise exc

    monkeypatch.setattr(tasks_mod, "IncidentPipeline", _FakePipeline)

    with pytest.raises(type(exc)):
        asyncio.run(tasks_mod.async_process_incident(
            {"incident_id": "RETRY-1"}, retries=retries, max_retries=max_retries,
        ))

    verify = Session()
    row = verify.query(IncidentRecord).filter_by(incident_id="RETRY-1").first()
    # Detach a snapshot before closing.
    snapshot = MagicMock()
    snapshot.status = row.status
    snapshot.analysis = row.analysis
    verify.close()
    return snapshot


def test_transient_error_below_max_retries_does_not_mark_failed(shared_engine, monkeypatch):
    """ConnectionError with retries remaining → status untouched, no post-mortem.

    Leaving the row non-terminal lets the Celery autoretry resume instead of
    dying on an invalid FAILED→INVESTIGATING transition.
    """
    row = _run_with_pipeline_error(
        shared_engine, monkeypatch,
        exc=ConnectionError("redis down"), retries=0, max_retries=3,
    )
    assert row.status == IncidentState.OPEN.value       # NOT FAILED
    assert row.analysis is None                          # no post-mortem


def test_transient_error_on_final_attempt_marks_failed(shared_engine, monkeypatch):
    """ConnectionError on the last attempt (retries == max_retries) → FAILED."""
    row = _run_with_pipeline_error(
        shared_engine, monkeypatch,
        exc=ConnectionError("still down"), retries=3, max_retries=3,
    )
    assert row.status == IncidentState.FAILED.value
    assert row.analysis["failed"]["error_type"] == "ConnectionError"


def test_non_retriable_error_marks_failed_even_with_retries_left(shared_engine, monkeypatch):
    """Non-retriable exception is terminal immediately (Celery won't retry it),
    so FAILED + post-mortem are recorded even at retries=0."""
    row = _run_with_pipeline_error(
        shared_engine, monkeypatch,
        exc=ValueError("bad payload"), retries=0, max_retries=3,
    )
    assert row.status == IncidentState.FAILED.value
    assert row.analysis["failed"]["error_type"] == "ValueError"


def test_retriable_exc_tuple_is_shared_with_autoretry():
    """The terminal decision reuses the exact tuple Celery autoretries on."""
    from app.workers.tasks import RETRIABLE_EXC, process_incident_task

    assert ConnectionError in RETRIABLE_EXC
    # Same object drives Celery's autoretry_for → cannot drift.
    assert process_incident_task.autoretry_for == RETRIABLE_EXC
