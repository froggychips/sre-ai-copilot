"""State machine integration tests.

Verify that `async_process_incident` drives the IncidentRecord through
the full StateMachine sequence (OPEN → INVESTIGATING → HYPOTHESIS_GENERATED
→ FIX_PROPOSED → RESOLVED), records `state_after` in the trace at the
stage where each transition fires, falls back to FAILED on any agent
exception, and accepts the legacy "PENDING" status as a starting point.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.state_machine import IncidentState, StateMachine
from app.workers.tasks import transition_to, _current_state


# MARK: - Helpers (mirror fixtures from test_pipeline_trace.py)

@pytest.fixture
def incident_data() -> dict:
    return {
        "incident_id": "STATE-TEST-1",
        "severity": "warning",
        "status": "firing",
        "summary": "x",
        "description": "y",
        "namespace": "squad-1",
        "labels": {"alertname": "X"},
        "annotations": {},
        "starts_at": "2026-05-08T12:00:00Z",
        "ends_at": None,
        "generator_url": None,
        "raw": {},
    }


@pytest.fixture
def happy_path_dependencies(mocker):
    mocker.patch("app.workers.tasks.AnalyzerAgent.analyze", new_callable=AsyncMock, return_value="a")
    mocker.patch("app.workers.tasks.HypothesisAgent.generate", new_callable=AsyncMock, return_value="h")
    mocker.patch("app.workers.tasks.CriticAgent.audit", new_callable=AsyncMock, return_value="c")
    mocker.patch("app.workers.tasks.FixAgent.suggest", new_callable=AsyncMock, return_value="f")
    mocker.patch("app.workers.tasks.RiskAgent.assess", new_callable=AsyncMock, return_value="r")
    mocker.patch("app.workers.tasks.SynthesisAgent.synthesize", new_callable=AsyncMock, return_value="s")
    mocker.patch("app.workers.tasks.SimilarIncidentEngine.find", return_value=[])
    mocker.patch("app.workers.tasks.discord_service.send_report", new_callable=AsyncMock)
    mocker.patch("app.workers.tasks.audit_service.log_event")

    record = MagicMock()
    record.trace = None
    record.analysis = None
    record.status = IncidentState.OPEN.value
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = record
    mocker.patch("app.workers.tasks.SessionLocal", return_value=mock_session)
    return {"record": record, "session": mock_session}


# MARK: - transition_to / _current_state unit tests

def test_current_state_parses_enum_value():
    rec = MagicMock()
    rec.status = "INVESTIGATING"
    assert _current_state(rec) == IncidentState.INVESTIGATING


def test_current_state_legacy_pending_aliased_to_open():
    rec = MagicMock()
    rec.status = "PENDING"
    assert _current_state(rec) == IncidentState.OPEN


def test_current_state_legacy_completed_aliased_to_resolved():
    rec = MagicMock()
    rec.status = "COMPLETED"
    assert _current_state(rec) == IncidentState.RESOLVED


def test_current_state_unknown_falls_back_to_open():
    rec = MagicMock()
    rec.status = "UNKNOWN_VALUE_FROM_OLD_RUN"
    assert _current_state(rec) == IncidentState.OPEN


def test_current_state_empty_falls_back_to_open():
    rec = MagicMock()
    rec.status = None
    assert _current_state(rec) == IncidentState.OPEN


def test_transition_to_writes_status_and_commits():
    rec = MagicMock()
    rec.status = IncidentState.OPEN.value
    db = MagicMock()
    transition_to(rec, IncidentState.INVESTIGATING, db)
    assert rec.status == IncidentState.INVESTIGATING.value
    db.commit.assert_called_once()


def test_transition_to_rejects_invalid_jump():
    rec = MagicMock()
    rec.status = IncidentState.OPEN.value
    db = MagicMock()
    # OPEN → RESOLVED is not in the StateMachine transitions table.
    with pytest.raises(ValueError, match="Invalid state transition"):
        transition_to(rec, IncidentState.RESOLVED, db)
    # Status must NOT have changed on failed validation.
    assert rec.status == IncidentState.OPEN.value
    db.commit.assert_not_called()


def test_transition_to_accepts_legacy_pending_as_open():
    """Webhook rows from before this PR start with status="PENDING"."""
    rec = MagicMock()
    rec.status = "PENDING"
    db = MagicMock()
    # PENDING aliased to OPEN, so OPEN → INVESTIGATING is allowed.
    transition_to(rec, IncidentState.INVESTIGATING, db)
    assert rec.status == IncidentState.INVESTIGATING.value


# MARK: - StateMachine.TRANSITIONS sanity

def test_failed_is_reachable_from_every_non_terminal_state():
    """The orchestrator's except-block depends on this invariant."""
    for state in IncidentState:
        if state in (IncidentState.RESOLVED, IncidentState.FAILED):
            continue
        assert StateMachine.validate_transition(state, IncidentState.FAILED), (
            f"FAILED must be reachable from {state.value}"
        )


def test_terminal_states_have_no_outgoing_transitions():
    assert StateMachine.TRANSITIONS[IncidentState.RESOLVED] == set()
    assert StateMachine.TRANSITIONS[IncidentState.FAILED] == set()


# MARK: - Pipeline integration: full happy path

@pytest.mark.asyncio
async def test_pipeline_drives_full_state_sequence(happy_path_dependencies, incident_data):
    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)

    record = happy_path_dependencies["record"]
    # Final state: RESOLVED.
    assert record.status == IncidentState.RESOLVED.value

    # Trace should mark transitions on the stages that triggered them.
    trace_by_stage = {entry["stage"]: entry for entry in record.trace}
    assert trace_by_stage["analyzer"].get("state_after") == IncidentState.INVESTIGATING.value
    assert trace_by_stage["hypothesis"].get("state_after") == IncidentState.HYPOTHESIS_GENERATED.value
    # Critic stays in HYPOTHESIS_GENERATED — no transition expected.
    assert "state_after" not in trace_by_stage["critic"]
    assert trace_by_stage["fix"].get("state_after") == IncidentState.FIX_PROPOSED.value
    # Risk stays in FIX_PROPOSED.
    assert "state_after" not in trace_by_stage["risk"]
    assert trace_by_stage["synthesis"].get("state_after") == IncidentState.RESOLVED.value


@pytest.mark.asyncio
async def test_pipeline_accepts_legacy_pending_status(mocker, happy_path_dependencies, incident_data):
    """Rows pre-created by old webhook code start with 'PENDING'."""
    happy_path_dependencies["record"].status = "PENDING"

    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)

    assert happy_path_dependencies["record"].status == IncidentState.RESOLVED.value


# MARK: - Pipeline integration: failure path

@pytest.mark.asyncio
async def test_pipeline_marks_failed_on_agent_exception(mocker, happy_path_dependencies, incident_data):
    """Any agent raising should land the record in FAILED, not stuck mid-way."""
    mocker.patch(
        "app.workers.tasks.FixAgent.suggest",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM exploded mid-fix"),
    )

    from app.workers.tasks import async_process_incident
    with pytest.raises(RuntimeError):
        await async_process_incident(incident_data)

    # The record was in FIX_PROPOSED's predecessor (HYPOTHESIS_GENERATED)
    # when the exception fired. The except-block transitions it to FAILED.
    record = happy_path_dependencies["record"]
    assert record.status == IncidentState.FAILED.value


@pytest.mark.asyncio
async def test_pipeline_handles_no_record_gracefully(mocker, incident_data):
    """When webhook didn't pre-create the row (e.g. ad-hoc CLI run),
    the worker shouldn't crash — it just skips state writes."""
    mocker.patch("app.workers.tasks.AnalyzerAgent.analyze", new_callable=AsyncMock, return_value="a")
    mocker.patch("app.workers.tasks.HypothesisAgent.generate", new_callable=AsyncMock, return_value="h")
    mocker.patch("app.workers.tasks.CriticAgent.audit", new_callable=AsyncMock, return_value="c")
    mocker.patch("app.workers.tasks.FixAgent.suggest", new_callable=AsyncMock, return_value="f")
    mocker.patch("app.workers.tasks.RiskAgent.assess", new_callable=AsyncMock, return_value="r")
    mocker.patch("app.workers.tasks.SynthesisAgent.synthesize", new_callable=AsyncMock, return_value="s")
    mocker.patch("app.workers.tasks.SimilarIncidentEngine.find", return_value=[])
    mocker.patch("app.workers.tasks.discord_service.send_report", new_callable=AsyncMock)
    mocker.patch("app.workers.tasks.audit_service.log_event")

    mock_session = MagicMock()
    # Simulate no pre-existing row.
    mock_session.query.return_value.filter.return_value.first.return_value = None
    mocker.patch("app.workers.tasks.SessionLocal", return_value=mock_session)

    from app.workers.tasks import async_process_incident
    # Must not raise.
    await async_process_incident(incident_data)
