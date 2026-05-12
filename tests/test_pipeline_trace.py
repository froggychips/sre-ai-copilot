"""Pipeline-level integration: 6 stages produce a 6-entry trace, and the
SimilarIncidentEngine output flows into HypothesisAgent.

All LLM-calling agents are mocked at the class-method level — the test
shouldn't make any network round-trip. The DB session is mocked at
`SessionLocal` so the test runs without Postgres.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def incident_data() -> dict:
    return {
        "incident_id": "TEST-INC-1",
        "severity": "warning",
        "status": "firing",
        "summary": "High error rate on api-7d8f",
        "description": "Sustained 5xx rate from api-7d8f over 10m.",
        "namespace": "squad-1",
        "labels": {"alertname": "HighErrorRate", "namespace": "squad-1"},
        "annotations": {"summary": "Error rate above 5%"},
        "starts_at": "2026-05-08T12:00:00Z",
        "ends_at": None,
        "generator_url": None,
        "raw": {},
    }


@pytest.fixture
def mocked_dependencies(mocker):
    """Mock all the things `async_process_incident` reaches into."""
    # All 6 agent class methods → simple AsyncMock that returns a string.
    mocker.patch(
        "app.workers.tasks.AnalyzerAgent.analyze",
        new_callable=AsyncMock,
        return_value="analysis text",
    )
    mocker.patch(
        "app.workers.tasks.HypothesisAgent.generate",
        new_callable=AsyncMock,
        return_value="hyp text",
    )
    mocker.patch(
        "app.workers.tasks.CriticAgent.audit",
        new_callable=AsyncMock,
        return_value="cause text",
    )
    mocker.patch(
        "app.workers.tasks.FixAgent.suggest",
        new_callable=AsyncMock,
        return_value="fix text",
    )
    mocker.patch(
        "app.workers.tasks.RiskAgent.assess",
        new_callable=AsyncMock,
        return_value="risk text",
    )
    mocker.patch(
        "app.workers.tasks.SynthesisAgent.synthesize",
        new_callable=AsyncMock,
        return_value="synth text",
    )

    # Similar incidents — empty by default; individual tests override.
    similar_mock = mocker.patch(
        "app.workers.tasks.SimilarIncidentEngine.find",
        return_value=[],
    )

    # Discord and audit — no-op.
    mocker.patch(
        "app.workers.tasks.discord_service.send_report", new_callable=AsyncMock
    )
    mocker.patch("app.workers.tasks.audit_service.log_event")

    # DB session: in-process MagicMock. The pipeline does query().filter().first(),
    # then mutates the returned record and calls commit(). Pre-seed status with
    # the enum's OPEN value so the StateMachine accepts the first transition
    # to INVESTIGATING (PENDING is also accepted via legacy alias).
    from app.core.state_machine import IncidentState
    record = MagicMock()
    record.trace = None
    record.analysis = None
    record.status = IncidentState.OPEN.value
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = record
    mocker.patch("app.workers.tasks.SessionLocal", return_value=mock_session)

    return {
        "record": record,
        "session": mock_session,
        "similar_find": similar_mock,
    }


@pytest.mark.asyncio
async def test_pipeline_writes_six_stage_trace(mocked_dependencies, incident_data):
    from app.workers.tasks import async_process_incident

    await async_process_incident(incident_data)

    record = mocked_dependencies["record"]
    assert record.trace is not None
    assert isinstance(record.trace, list)
    assert len(record.trace) == 6
    stages = [s["stage"] for s in record.trace]
    assert stages == ["analyzer", "hypothesis", "critic", "fix", "risk", "synthesis"]

    for entry in record.trace:
        assert "duration_ms" in entry
        assert "llm_calls" in entry
        assert isinstance(entry["duration_ms"], int)
        assert entry["duration_ms"] >= 0
        # Mocked agents bypass BaseAgent.ask, so they don't push llm_calls.
        # Empty list is the expected (and tested) shape.
        assert entry["llm_calls"] == []


@pytest.mark.asyncio
async def test_pipeline_passes_similar_past_to_hypothesis(
    mocker, mocked_dependencies, incident_data
):
    mocked_dependencies["similar_find"].return_value = [
        {
            "incident_id": "OLD-1",
            "score": 0.7,
            "root_cause": "memory limit",
            "summary": "OOM",
        },
    ]
    # Re-patch HypothesisAgent.generate so we can inspect its call args.
    hypo_mock = mocker.patch(
        "app.workers.tasks.HypothesisAgent.generate",
        new_callable=AsyncMock,
        return_value="hyp",
    )

    from app.workers.tasks import async_process_incident

    await async_process_incident(incident_data)

    # SimilarIncidentEngine.find was called with the incident_data dict
    mocked_dependencies["similar_find"].assert_called_once()
    called_with = mocked_dependencies["similar_find"].call_args
    assert called_with.kwargs.get("current_incident") == incident_data
    assert called_with.kwargs.get("limit") == 3

    # HypothesisAgent.generate was called with similar_past forwarded.
    assert hypo_mock.await_count == 1
    forwarded_similar = hypo_mock.call_args.kwargs["similar_past"]
    assert len(forwarded_similar) == 1
    assert forwarded_similar[0]["incident_id"] == "OLD-1"


@pytest.mark.asyncio
async def test_pipeline_stores_similar_past_count_in_analysis(
    mocked_dependencies, incident_data
):
    mocked_dependencies["similar_find"].return_value = [
        {"incident_id": "OLD-1", "score": 0.7, "root_cause": "x", "summary": "y"},
        {"incident_id": "OLD-2", "score": 0.5, "root_cause": "a", "summary": "b"},
    ]

    from app.workers.tasks import async_process_incident

    await async_process_incident(incident_data)

    analysis = mocked_dependencies["record"].analysis
    assert analysis is not None
    assert analysis["similar_past_count"] == 2


@pytest.mark.asyncio
async def test_pipeline_marks_resolved_and_commits(mocked_dependencies, incident_data):
    from app.workers.tasks import async_process_incident
    from app.core.state_machine import IncidentState

    await async_process_incident(incident_data)

    # Was "COMPLETED" before StateMachine wiring; now the pipeline drives
    # the row through OPEN → INVESTIGATING → HYPOTHESIS_GENERATED →
    # FIX_PROPOSED → RESOLVED and the final status is RESOLVED.
    assert mocked_dependencies["record"].status == IncidentState.RESOLVED.value
    mocked_dependencies["session"].commit.assert_called()
