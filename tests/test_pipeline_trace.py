"""Pipeline-level integration: новый fact-anchored поток через 7 стадий.

Стадии после интеграции multi_hypothesis + fact_critic:
    analyzer → diagnostics → hypothesis → critic → fix → risk → synthesis

В отличие от старого pipeline-а тут:
    * MultiHypothesisAgent заменяет HypothesisAgent (контракт другой)
    * FactCriticAgent заменяет CriticAgent (контракт другой)
    * Между analyzer и hypothesis вставлена stage `diagnostics`, которая
      ничего LLM-вого не делает (DiagnosticEngine.run — синхронно)
    * SimilarIncidentEngine.find остался и подмешивается в incident_summary

LLM-агенты замокирваны на уровне класса. DB session — MagicMock.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.core.state_machine import IncidentState
from app.diagnostics.facts import Fact, FactKind


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


def _make_hypothesis_set() -> HypothesisSet:
    return HypothesisSet(items=[
        Hypothesis(
            cause="OOM on api-7d8f",
            anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.9,
            perspective="infra",
        ),
    ])


def _make_critiqued_set() -> HypothesisSet:
    # Та же гипотеза, без refutations — survivor.
    return _make_hypothesis_set()


@pytest.fixture
def mocked_dependencies(mocker):
    """Замокировать всё, в что лезет `async_process_incident`."""
    # AnalyzerAgent / FixAgent / RiskAgent / SynthesisAgent — старые контракты.
    mocker.patch(
        "app.workers.pipeline.AnalyzerAgent.analyze",
        new_callable=AsyncMock,
        return_value="analysis text",
    )
    mocker.patch(
        "app.workers.pipeline.FixAgent.suggest",
        new_callable=AsyncMock,
        return_value="fix text",
    )
    mocker.patch(
        "app.workers.pipeline.RiskAgent.assess",
        new_callable=AsyncMock,
        return_value="risk text",
    )
    mocker.patch(
        "app.workers.pipeline.SynthesisAgent.synthesize",
        new_callable=AsyncMock,
        return_value="synth text",
    )

    # MultiHypothesisAgent + FactCriticAgent — новые контракты.
    mocker.patch(
        "app.workers.pipeline.MultiHypothesisAgent.generate",
        new_callable=AsyncMock,
        return_value=_make_hypothesis_set(),
    )
    mocker.patch(
        "app.workers.pipeline.FactCriticAgent.critique_all",
        new_callable=AsyncMock,
        return_value=_make_critiqued_set(),
    )

    # SimilarIncidentEngine остаётся как enrichment hypothesis-стадии.
    similar_mock = mocker.patch(
        "app.workers.pipeline.SimilarIncidentEngine.find",
        return_value=[],
    )

    # DiagnosticEngine — синхронный, мокать не обязательно: на пустом ctx
    # отдаёт серию observed=False фактов. Но для предсказуемости подсунем
    # FactStore с одним observed-фактом, чтобы grounded-фильтр прошёл.
    diag_store_mock = mocker.patch(
        "app.workers.pipeline.diag_engine.run",
        return_value=__make_fact_store(),
    )

    mocker.patch(
        "app.workers.pipeline.discord_service.send_report",
        new_callable=AsyncMock,
    )
    mocker.patch("app.workers.pipeline.audit_service.log_event")

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
        "diag_run": diag_store_mock,
    }


def __make_fact_store():
    """FactStore с одним observed: oom_killed — ровно как мок hypothesis сделал anchor."""
    from app.diagnostics.facts import FactStore
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95)
    ])


@pytest.mark.asyncio
async def test_pipeline_writes_seven_stage_trace(mocked_dependencies, incident_data):
    from app.workers.tasks import async_process_incident

    await async_process_incident(incident_data)

    record = mocked_dependencies["record"]
    assert record.trace is not None
    assert isinstance(record.trace, list)
    assert len(record.trace) == 7
    stages = [s["stage"] for s in record.trace]
    assert stages == [
        "analyzer", "diagnostics", "hypothesis", "critic",
        "fix", "risk", "synthesis"
    ]

    for entry in record.trace:
        assert "duration_ms" in entry
        assert "llm_calls" in entry
        assert isinstance(entry["duration_ms"], int)
        assert entry["duration_ms"] >= 0
        # Mocked agents bypass BaseAgent.ask, so они не пушат llm_calls.
        assert entry["llm_calls"] == []


@pytest.mark.asyncio
async def test_pipeline_passes_similar_past_to_hypothesis(
    mocker, mocked_dependencies, incident_data
):
    """SimilarIncidentEngine.find подмешивается в incident_summary для MultiHypothesisAgent."""
    mocked_dependencies["similar_find"].return_value = [
        {
            "incident_id": "OLD-1",
            "score": 0.7,
            "root_cause": "memory limit was too low",
            "summary": "OOM",
        },
    ]
    # Re-patch MultiHypothesisAgent.generate, чтобы прочитать его call args.
    multi_mock = mocker.patch(
        "app.workers.pipeline.MultiHypothesisAgent.generate",
        new_callable=AsyncMock,
        return_value=_make_hypothesis_set(),
    )

    from app.workers.tasks import async_process_incident
    await async_process_incident(incident_data)

    mocked_dependencies["similar_find"].assert_called_once()
    called_with = mocked_dependencies["similar_find"].call_args
    assert called_with.kwargs.get("current_incident") == incident_data
    assert called_with.kwargs.get("limit") == 3

    # incident_summary, переданный в MultiHypothesisAgent, должен содержать
    # past-cause фразу.
    assert multi_mock.await_count == 1
    passed_summary = multi_mock.call_args.kwargs["incident_summary"]
    assert "memory limit was too low" in passed_summary


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

    await async_process_incident(incident_data)

    # Pipeline должен довести инцидент до RESOLVED через
    # OPEN → INVESTIGATING → FACTS_COLLECTED → HYPOTHESIS_GENERATED →
    # FIX_PROPOSED → RESOLVED.
    assert mocked_dependencies["record"].status == IncidentState.RESOLVED.value
    mocked_dependencies["session"].commit.assert_called()


@pytest.mark.asyncio
async def test_pipeline_persists_fact_anchored_details(mocked_dependencies, incident_data):
    """В record.analysis должны лежать facts + hypothesis_set + best_candidate."""
    from app.workers.tasks import async_process_incident

    await async_process_incident(incident_data)
    analysis = mocked_dependencies["record"].analysis
    assert "facts" in analysis
    assert isinstance(analysis["facts"], list)
    assert "hypothesis_set" in analysis
    assert len(analysis["hypothesis_set"]) >= 1
    assert "best_candidate" in analysis
    assert analysis["best_candidate"]["cause"] == "OOM on api-7d8f"
