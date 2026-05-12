"""Тесты на fan-out hypothesis + fact-anchoring."""
from unittest.mock import patch

import pytest

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.agents.multi_hypothesis import (PERSPECTIVE_PRECONDITIONS, PERSPECTIVES,
                                         MultiHypothesisAgent, PerspectiveAgent,
                                         _parse_hypotheses)
from app.diagnostics.facts import Fact, FactKind, FactStore


# ---------- Hypothesis model invariants -----------------------------------

def test_hypothesis_rejects_empty_anchor():
    with pytest.raises(ValueError, match="at least one anchor"):
        Hypothesis(
            cause="x", anchored_facts=[], confidence=0.5, perspective="app"
        )


def test_hypothesis_set_filter_grounded_drops_unobserved_kinds():
    h1 = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    h2 = Hypothesis(
        cause="Mythical kind", anchored_facts=["unicorn_explosion"],
        confidence=0.8, perspective="app",
    )
    h3 = Hypothesis(
        cause="Mixed",
        anchored_facts=[FactKind.OOM_KILLED, "unicorn_explosion"],
        confidence=0.7, perspective="deps",
    )
    s = HypothesisSet(items=[h1, h2, h3])
    out = s.filter_grounded({FactKind.OOM_KILLED})
    # h2 выкинута целиком, h3 сохранена но с urезанным anchored_facts.
    assert {h.cause for h in out.items} == {"OOM", "Mixed"}
    mixed = next(h for h in out.items if h.cause == "Mixed")
    assert mixed.anchored_facts == [FactKind.OOM_KILLED]


def test_consensus_kinds_requires_two_perspectives():
    s = HypothesisSet(items=[
        Hypothesis(cause="a", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
        Hypothesis(cause="b", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.8, perspective="app"),
        Hypothesis(cause="c", anchored_facts=[FactKind.RECENT_DEPLOY],
                   confidence=0.7, perspective="app"),  # один perspective only
    ])
    consensus = s.consensus_kinds()
    assert FactKind.OOM_KILLED in consensus
    assert FactKind.RECENT_DEPLOY not in consensus


def test_disagreement_signal_when_perspectives_diverge():
    s = HypothesisSet(items=[
        Hypothesis(cause="app says deploy",
                   anchored_facts=[FactKind.RECENT_DEPLOY],
                   confidence=0.9, perspective="app"),
        Hypothesis(cause="infra says oom",
                   anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
        Hypothesis(cause="deps says upstream",
                   anchored_facts=[FactKind.UPSTREAM_DEGRADED],
                   confidence=0.9, perspective="deps"),
    ])
    assert s.disagreement_signal() is not None


def test_no_disagreement_when_perspectives_agree():
    s = HypothesisSet(items=[
        Hypothesis(cause="app",
                   anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="app"),
        Hypothesis(cause="infra",
                   anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
    ])
    assert s.disagreement_signal() is None


# ---------- _parse_hypotheses -------------------------------------------

def test_parse_valid_json():
    raw = (
        '{"hypotheses":['
        '{"cause":"OOM","detail":"d","anchored_facts":["oom_killed"],"confidence":0.9}'
        ']}'
    )
    out = _parse_hypotheses(raw, perspective="infra")
    assert len(out) == 1
    assert out[0].perspective == "infra"
    assert out[0].cause == "OOM"


def test_parse_strips_markdown_fence():
    raw = "```json\n" + '{"hypotheses":[{"cause":"c","anchored_facts":["oom_killed"],"confidence":0.5}]}' + "\n```"
    out = _parse_hypotheses(raw, perspective="app")
    assert len(out) == 1


def test_parse_invalid_json_returns_empty():
    assert _parse_hypotheses("this is not json", perspective="app") == []


def test_parse_skips_invalid_hypothesis_in_array():
    """Один элемент битый, второй валидный — возвращаем валидный."""
    raw = (
        '{"hypotheses":['
        '{"cause":"no anchor","anchored_facts":[],"confidence":0.5},'
        '{"cause":"ok","anchored_facts":["oom_killed"],"confidence":0.7}'
        ']}'
    )
    out = _parse_hypotheses(raw, perspective="infra")
    assert len(out) == 1
    assert out[0].cause == "ok"


def test_parse_empty_input_returns_empty():
    assert _parse_hypotheses("", perspective="x") == []
    assert _parse_hypotheses("   ", perspective="x") == []


# ---------- MultiHypothesisAgent ----------------------------------------

@pytest.fixture
def facts_oom_only():
    """FactStore с одним observed: oom_killed."""
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95,
             evidence={"hits": 1}),
        Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.9),
    ])


@pytest.mark.asyncio
async def test_orchestrator_fans_out_and_grounds(facts_oom_only):
    """OOM-инцидент: 3 perspective активны (runtime пропускается — нет process_crash)."""
    captured = {"calls": []}

    async def fake_ask(self, user_context, instruction=""):
        captured["calls"].append(self.name)
        return (
            '{"hypotheses":[{"cause":"OOM on '
            + self.name
            + '","anchored_facts":["oom_killed"],"confidence":0.8}]}'
        )

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        agent = MultiHypothesisAgent()
        result = await agent.generate(
            incident_summary="pod stub-1 OOMKilled", facts=facts_oom_only
        )

    # runtime пропущен — нет process_crash в observed.
    assert set(captured["calls"]) == {
        "Hypothesis-app", "Hypothesis-infra", "Hypothesis-deps"
    }
    assert len(result.items) == 3
    for h in result.items:
        assert h.anchored_facts == ["oom_killed"]


@pytest.mark.asyncio
async def test_orchestrator_filters_unobserved_anchors(facts_oom_only):
    """LLM ссылается на kind, которого нет — гипотеза отбрасывается."""
    async def fake_ask(self, user_context, instruction=""):
        # Все perspective пытаются опереться на recent_deploy (а он ✗).
        return (
            '{"hypotheses":[{"cause":"x","anchored_facts":["recent_deploy"],'
            '"confidence":0.9}]}'
        )

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        agent = MultiHypothesisAgent()
        result = await agent.generate(
            incident_summary="anything", facts=facts_oom_only
        )

    assert result.items == []  # все perspective выкинуло


@pytest.mark.asyncio
async def test_orchestrator_survives_perspective_exception(facts_oom_only):
    """Один perspective упал — остальные продолжают."""
    async def flaky_ask(self, user_context, instruction=""):
        if self.name == "Hypothesis-deps":
            raise RuntimeError("LLM provider 500")
        return (
            '{"hypotheses":[{"cause":"ok","anchored_facts":["oom_killed"],'
            '"confidence":0.7}]}'
        )

    with patch("app.agents.base.BaseAgent.ask", new=flaky_ask):
        agent = MultiHypothesisAgent()
        result = await agent.generate(
            incident_summary="x", facts=facts_oom_only
        )

    perspectives = {h.perspective for h in result.items}
    assert "deps" not in perspectives
    # runtime тоже пропущен — facts_oom_only не содержит process_crash.
    assert perspectives == {"app", "infra"}


@pytest.mark.asyncio
async def test_perspective_agent_validates_perspective_name():
    with pytest.raises(ValueError, match="unknown perspective"):
        PerspectiveAgent("marketing")


def test_perspectives_registry_unchanged():
    """Гайдрейл: если добавляешь новый perspective — синхронизируй тест."""
    assert set(PERSPECTIVES.keys()) == {"app", "infra", "deps", "runtime"}


def test_runtime_precondition_requires_process_crash():
    """runtime perspective требует process_crash в preconditions."""
    from app.diagnostics.facts import FactKind
    assert FactKind.PROCESS_CRASH in PERSPECTIVE_PRECONDITIONS["runtime"]


@pytest.mark.asyncio
async def test_runtime_skipped_without_process_crash():
    """Без process_crash в observed — runtime perspective не запускается."""
    captured = {"calls": []}

    async def fake_ask(self, user_context, instruction=""):
        captured["calls"].append(self.name)
        return '{"hypotheses":[{"cause":"x","anchored_facts":["oom_killed"],"confidence":0.8}]}'

    facts_no_crash = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        agent = MultiHypothesisAgent()
        await agent.generate(incident_summary="OOM incident", facts=facts_no_crash)

    assert "Hypothesis-runtime" not in captured["calls"]
    assert "Hypothesis-app" in captured["calls"]


@pytest.mark.asyncio
async def test_runtime_active_with_process_crash():
    """С process_crash в observed — runtime perspective запускается."""
    captured = {"calls": []}

    async def fake_ask(self, user_context, instruction=""):
        captured["calls"].append(self.name)
        return '{"hypotheses":[{"cause":"x","anchored_facts":["process_crash"],"confidence":0.8}]}'

    facts_with_crash = FactStore([
        Fact(kind=FactKind.PROCESS_CRASH, observed=True, confidence=0.97),
    ])

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        agent = MultiHypothesisAgent()
        await agent.generate(incident_summary="SIGSEGV crash", facts=facts_with_crash)

    assert "Hypothesis-runtime" in captured["calls"]
