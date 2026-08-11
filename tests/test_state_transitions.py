"""State machine integration tests.

Проверяем, что `async_process_incident` проводит IncidentRecord через
fact-anchored поток состояний:
    OPEN → INVESTIGATING → FACTS_COLLECTED → HYPOTHESIS_GENERATED
        → FIX_PROPOSED → RESOLVED

Фиксирует `state_after` в trace на стадии, где происходит transition,
сваливается в FAILED при exception от любого агента, принимает legacy
"PENDING" как стартовый статус.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.core.state_machine import IncidentState, StateMachine
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.workers.pipeline import _current_state, transition_to


# MARK: - Helpers

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


def _hyp_set():
    return HypothesisSet(items=[
        Hypothesis(cause="x", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
    ])


def _fact_store():
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95)
    ])


@pytest.fixture
def happy_path_dependencies(mocker):
    mocker.patch("app.workers.pipeline.AnalyzerAgent.analyze",
                 new_callable=AsyncMock, return_value="a")
    mocker.patch("app.workers.pipeline.MultiHypothesisAgent.generate",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FactCriticAgent.critique_all",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FixAgent.suggest",
                 new_callable=AsyncMock, return_value=("f", None))
    mocker.patch("app.workers.pipeline.RiskAgent.assess",
                 new_callable=AsyncMock, return_value="r")
    mocker.patch("app.workers.pipeline.SynthesisAgent.synthesize",
                 new_callable=AsyncMock, return_value="s")
    mocker.patch("app.workers.pipeline.SimilarIncidentEngine.find", return_value=[])
    mocker.patch("app.workers.pipeline.diag_engine.run", return_value=_fact_store())
    mocker.patch("app.workers.pipeline.discord_service.send_report",
                 new_callable=AsyncMock)
    # send_incident_report возвращает delivered (outbox в pipeline): без мока
    # тест бил живым POST-ом в example.com из conftest → 405 → delivered=False
    # → ReportDeliveryPending.
    mocker.patch("app.workers.pipeline.discord_service.send_incident_report",
                 new_callable=AsyncMock, return_value=True)
    mocker.patch("app.workers.pipeline.audit_service.log_event")

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
    # OPEN → FIX_PROPOSED: перепрыгнуть расследование нельзя.
    # (OPEN → RESOLVED теперь РАЗРЕШЁН намеренно — короткоживущий алерт,
    # погасший до начала расследования; см. TRANSITIONS в state_machine.)
    with pytest.raises(ValueError, match="Invalid state transition"):
        transition_to(rec, IncidentState.FIX_PROPOSED, db)
    assert rec.status == IncidentState.OPEN.value
    db.commit.assert_not_called()


def test_transition_to_accepts_legacy_pending_as_open():
    """Webhook rows from before this PR start with status="PENDING"."""
    rec = MagicMock()
    rec.status = "PENDING"
    db = MagicMock()
    transition_to(rec, IncidentState.INVESTIGATING, db)
    assert rec.status == IncidentState.INVESTIGATING.value


# MARK: - StateMachine.TRANSITIONS sanity

def test_failed_is_reachable_from_every_non_terminal_state():
    for state in IncidentState:
        if state in (
            IncidentState.RESOLVED,
            IncidentState.TRIAGE_REQUIRED,
            IncidentState.FAILED,
        ):
            continue
        assert StateMachine.validate_transition(state, IncidentState.FAILED), (
            f"FAILED must be reachable from {state.value}"
        )


def test_triage_required_is_terminal():
    assert StateMachine.TRANSITIONS[IncidentState.TRIAGE_REQUIRED] == set()


def test_terminal_states_have_no_outgoing_transitions():
    assert StateMachine.TRANSITIONS[IncidentState.RESOLVED] == set()
    assert StateMachine.TRANSITIONS[IncidentState.FAILED] == set()


def test_fact_anchored_path_is_valid():
    """OPEN → INVESTIGATING → FACTS_COLLECTED → HYPOTHESIS_GENERATED → FIX_PROPOSED → RESOLVED."""
    path = [
        IncidentState.OPEN,
        IncidentState.INVESTIGATING,
        IncidentState.FACTS_COLLECTED,
        IncidentState.HYPOTHESIS_GENERATED,
        IncidentState.FIX_PROPOSED,
        IncidentState.RESOLVED,
    ]
    for cur, nxt in zip(path, path[1:]):
        assert StateMachine.validate_transition(cur, nxt), (
            f"fact-anchored path broken at {cur.value} → {nxt.value}"
        )


def test_legacy_path_still_valid():
    assert StateMachine.validate_transition(
        IncidentState.INVESTIGATING, IncidentState.HYPOTHESIS_GENERATED
    )


def test_fix_proposed_allows_followup_investigating():
    """Multi-turn /copilot: FIX_PROPOSED → INVESTIGATING (follow-up вопрос).

    Каждый прогон generate_reply начинается с transition(INVESTIGATING),
    успешный прогон оставляет FIX_PROPOSED. Без этого перехода второе
    сообщение в диалог всегда падало и уводило state в FAILED (терминал).
    """
    assert StateMachine.validate_transition(
        IncidentState.FIX_PROPOSED, IncidentState.INVESTIGATING
    )


def test_cannot_skip_investigating_to_facts_collected():
    assert not StateMachine.validate_transition(
        IncidentState.OPEN, IncidentState.FACTS_COLLECTED
    )


def test_cannot_go_backwards_from_facts_collected():
    assert not StateMachine.validate_transition(
        IncidentState.FACTS_COLLECTED, IncidentState.INVESTIGATING
    )


# MARK: - Pipeline integration: full happy path

@pytest.mark.asyncio
async def test_pipeline_drives_full_state_sequence(happy_path_dependencies, incident_data):
    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)

    record = happy_path_dependencies["record"]
    assert record.status == IncidentState.RESOLVED.value

    trace_by_stage = {entry["stage"]: entry for entry in record.trace}
    assert trace_by_stage["analyzer"].get("state_after") == IncidentState.INVESTIGATING.value
    assert trace_by_stage["diagnostics"].get("state_after") == IncidentState.FACTS_COLLECTED.value
    # hypothesis сама state_after не двигает, transition в HYPOTHESIS_GENERATED
    # ставится после critic — отражено в critic stage.
    assert trace_by_stage["critic"].get("state_after") == IncidentState.HYPOTHESIS_GENERATED.value
    assert trace_by_stage["fix"].get("state_after") == IncidentState.FIX_PROPOSED.value
    # Risk остаётся в FIX_PROPOSED.
    assert "state_after" not in trace_by_stage["risk"]
    assert trace_by_stage["synthesis"].get("state_after") == IncidentState.RESOLVED.value


@pytest.mark.asyncio
async def test_pipeline_accepts_legacy_pending_status(happy_path_dependencies, incident_data):
    """Rows pre-created by old webhook code start with 'PENDING'."""
    happy_path_dependencies["record"].status = "PENDING"

    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)

    assert happy_path_dependencies["record"].status == IncidentState.RESOLVED.value


# MARK: - Pipeline integration: failure path

@pytest.mark.asyncio
async def test_pipeline_marks_failed_on_agent_exception(mocker, happy_path_dependencies, incident_data):
    mocker.patch(
        "app.workers.pipeline.FixAgent.suggest",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM exploded mid-fix"),
    )

    from app.workers.tasks import async_process_incident
    with pytest.raises(RuntimeError):
        await async_process_incident(incident_data)

    record = happy_path_dependencies["record"]
    assert record.status == IncidentState.FAILED.value


@pytest.mark.asyncio
async def test_pipeline_handles_no_record_gracefully(mocker, incident_data):
    """Webhook не пред-создал row (ad-hoc CLI run) — worker не должен крашиться."""
    mocker.patch("app.workers.pipeline.AnalyzerAgent.analyze",
                 new_callable=AsyncMock, return_value="a")
    mocker.patch("app.workers.pipeline.MultiHypothesisAgent.generate",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FactCriticAgent.critique_all",
                 new_callable=AsyncMock, return_value=_hyp_set())
    mocker.patch("app.workers.pipeline.FixAgent.suggest",
                 new_callable=AsyncMock, return_value=("f", None))
    mocker.patch("app.workers.pipeline.RiskAgent.assess",
                 new_callable=AsyncMock, return_value="r")
    mocker.patch("app.workers.pipeline.SynthesisAgent.synthesize",
                 new_callable=AsyncMock, return_value="s")
    mocker.patch("app.workers.pipeline.SimilarIncidentEngine.find", return_value=[])
    mocker.patch("app.workers.pipeline.diag_engine.run", return_value=_fact_store())
    mocker.patch("app.workers.pipeline.discord_service.send_report",
                 new_callable=AsyncMock)
    # send_incident_report возвращает delivered (outbox в pipeline): без мока
    # тест бил живым POST-ом в example.com из conftest → 405 → delivered=False
    # → ReportDeliveryPending.
    mocker.patch("app.workers.pipeline.discord_service.send_incident_report",
                 new_callable=AsyncMock, return_value=True)
    mocker.patch("app.workers.pipeline.audit_service.log_event")

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None
    mocker.patch("app.workers.tasks.SessionLocal", return_value=mock_session)

    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)
