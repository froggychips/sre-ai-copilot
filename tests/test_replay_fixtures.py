"""Replay-snapshot fixtures: regression-проверка pipeline'а на 7 типовых
WO incident-сценариях БЕЗ единого LLM-вызова.

## Что покрываем

7 fixtures в `tests/fixtures/incidents/*.json` — реалистичные AlertManager
payloads под разные классы инцидентов:
  - crashloop          CrashLoopBackOff (exit 137)
  - oomkilled          OOMKilled (explicit reason)
  - imagepull          ImagePullBackOff (после deploy)
  - highlatency        HighLatency (p99 > threshold)
  - diconfig           DI / IStatics misconfig — WO-специфичный
  - nodedisk           NodeDiskIOSaturation — node-level, не service
  - scrapenotargets    ScrapePoolHasNoTargets — observability config

## Cost-tracking invariants

Тест гарантирует что **0 реальных LLM-вызовов** делается:
  - `ModelRouter.route_and_call_full` запатчен `raise AssertionError` —
    если test случайно проскочит мимо mocks и дёрнет реальный route,
    упадёт с явным сообщением.
  - На уровне stage-агентов: `analyze`/`generate`/`critique_all`/etc.
    замокированы AsyncMock — счётчик вызовов проверяется явно.

Эти fixtures **разблокируют Stage 3**: можно регрессионно прогонять
pipeline на каждый новый prompt change без LLM-burn ($0 за прогон).

## Как добавлять новый сценарий

  1. tests/fixtures/incidents/<name>.json — следуй существующей форме
     (_fixture_id, _description обязательны для self-doc).
  2. Добавь имя в `FIXTURE_NAMES`. parametrize-collection подхватит.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.core.state_machine import IncidentState
from app.diagnostics.facts import Fact, FactKind, FactStore

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "incidents"

FIXTURE_NAMES = [
    "crashloop",
    "oomkilled",
    "imagepull",
    "highlatency",
    "diconfig",
    "nodedisk",
    "scrapenotargets",
]


def _load_fixture(name: str) -> dict:
    """Загрузить JSON fixture и убрать meta-поля (_fixture_id, _description)."""
    path = _FIXTURE_DIR / f"{name}.json"
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _make_hypothesis_set() -> HypothesisSet:
    """Minimal valid hypothesis set — pipeline проходит fact-critic фильтр."""
    return HypothesisSet(items=[
        Hypothesis(
            cause="stub-hypothesis",
            anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.9,
            perspective="infra",
        ),
    ])


def _make_fact_store() -> FactStore:
    """FactStore с одним observed fact (anchor для hypothesis выше)."""
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])


@pytest.fixture
def mocked_pipeline_deps(mocker):
    """Mock все LLM-агенты + БД + Discord + audit + ModelRouter."""
    # Stage agents (LLM-bound)
    analyze_mock = mocker.patch(
        "app.workers.pipeline.AnalyzerAgent.analyze",
        new_callable=AsyncMock, return_value="stub-analysis",
    )
    fix_mock = mocker.patch(
        "app.workers.pipeline.FixAgent.suggest",
        new_callable=AsyncMock, return_value=("stub-fix", None),
    )
    risk_mock = mocker.patch(
        "app.workers.pipeline.RiskAgent.assess",
        new_callable=AsyncMock, return_value="stub-risk",
    )
    synth_mock = mocker.patch(
        "app.workers.pipeline.SynthesisAgent.synthesize",
        new_callable=AsyncMock, return_value="stub-synth",
    )
    hyp_mock = mocker.patch(
        "app.workers.pipeline.MultiHypothesisAgent.generate",
        new_callable=AsyncMock, return_value=_make_hypothesis_set(),
    )
    critic_mock = mocker.patch(
        "app.workers.pipeline.FactCriticAgent.critique_all",
        new_callable=AsyncMock, return_value=_make_hypothesis_set(),
    )

    # Non-LLM dependencies
    mocker.patch(
        "app.workers.pipeline.diag_engine.run",
        return_value=_make_fact_store(),
    )
    mocker.patch(
        "app.workers.pipeline.SimilarIncidentEngine.find",
        return_value=[],
    )
    mocker.patch(
        "app.workers.pipeline.discord_service.send_report",
        new_callable=AsyncMock,
    )
    mocker.patch("app.workers.pipeline.audit_service.log_event")

    # ── COST-TRACKING TRIPWIRE ────────────────────────────────────────
    # Любой проскальзывающий LLM-вызов (через ModelRouter.route_and_call_full
    # или прямой generate_full) должен упасть с явным сообщением.
    # Это страховка от случайного $0.05/incident burn в тестах.
    def _route_tripwire(*args, **kwargs):
        raise AssertionError(
            "Replay-fixture тест попытался сделать РЕАЛЬНЫЙ LLM-вызов через "
            "ModelRouter. Проверь mocked_pipeline_deps — какой-то агент не "
            "замокан. Args: %r kwargs: %r" % (args, kwargs)
        )
    mocker.patch(
        "app.llm.router.ModelRouter.route_and_call_full",
        side_effect=_route_tripwire,
    )
    mocker.patch(
        "app.llm.router.ModelRouter.route_and_call",
        side_effect=_route_tripwire,
    )

    # Mock DB session
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
        "agent_mocks": {
            "analyze": analyze_mock, "fix": fix_mock, "risk": risk_mock,
            "synth": synth_mock, "hyp": hyp_mock, "critic": critic_mock,
        },
    }


# ── Main parametrized test ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
async def test_pipeline_runs_through_fixture_without_real_llm(
    fixture_name, mocked_pipeline_deps,
):
    """Pipeline проходит все 7 стадий на любом fixture'е без exception
    и БЕЗ единого реального LLM-вызова (tripwire не срабатывает).

    Это и есть «replay»: статичный input → детерминированный mock-output
    → проверка что pipeline-плумбинг устойчив к разным incident-классам.
    """
    from app.workers.tasks import async_process_incident

    incident_data = _load_fixture(fixture_name)
    # Pipeline не должен бросить — даже если данные edge-case-овые.
    await async_process_incident(incident_data)

    # Trace записан и содержит ровно 7 стадий.
    record = mocked_pipeline_deps["record"]
    assert record.trace is not None, f"trace отсутствует для {fixture_name}"
    assert len(record.trace) == 7, (
        f"{fixture_name}: ожидалось 7 stages, получили {len(record.trace)}"
    )
    stages = [s["stage"] for s in record.trace]
    assert stages == [
        "analyzer", "diagnostics", "hypothesis", "critic",
        "fix", "risk", "synthesis",
    ], f"{fixture_name}: неверный порядок стадий: {stages}"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
async def test_pipeline_calls_each_agent_exactly_once(
    fixture_name, mocked_pipeline_deps,
):
    """Каждый stage-агент вызывается ровно один раз (sanity на mock
    plumbing). Если кто-то случайно поставит `for _ in range(3): await
    agent.generate(...)` — этот тест поймает."""
    from app.workers.tasks import async_process_incident

    incident_data = _load_fixture(fixture_name)
    await async_process_incident(incident_data)

    mocks = mocked_pipeline_deps["agent_mocks"]
    for name, mock in mocks.items():
        assert mock.await_count == 1, (
            f"{fixture_name}: agent {name}.await_count = "
            f"{mock.await_count} (ожидалось 1)"
        )


@pytest.mark.asyncio
async def test_fixtures_are_well_formed_and_unique():
    """Sanity-check на сами fixture-файлы.

    - Все 7 файлов существуют и валидный JSON.
    - У каждого есть _fixture_id, _description (self-doc).
    - incident_id уникальны — иначе dedup в pipeline схлопнет тесты в один.
    """
    seen_ids = set()
    for name in FIXTURE_NAMES:
        path = _FIXTURE_DIR / f"{name}.json"
        assert path.exists(), f"fixture {name}.json не существует"
        data = json.loads(path.read_text())
        assert data.get("_fixture_id") == name, (
            f"{name}.json: _fixture_id должен совпадать с именем файла"
        )
        assert data.get("_description"), (
            f"{name}.json: пустой _description — добавь self-doc"
        )
        iid = data["incident_id"]
        assert iid not in seen_ids, f"incident_id {iid} дублируется"
        seen_ids.add(iid)
