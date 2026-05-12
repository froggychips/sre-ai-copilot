"""Тесты на fact-based adversarial critic."""
from unittest.mock import patch

import pytest

from app.agents.fact_critic import (FactCriticAgent, _algorithmic_refutations,
                                    _parse_refutations, best_candidate,
                                    refuted, survivors)
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.diagnostics.facts import Fact, FactKind, FactStore


@pytest.fixture
def facts_oom_observed():
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.9),
    ])


@pytest.fixture
def facts_weak_oom():
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.3),  # слабый
    ])


# ---------- _algorithmic_refutations ------------------------------------

def test_algo_flags_unobserved_anchor():
    h = Hypothesis(
        cause="x", anchored_facts=["nonexistent_kind"],
        confidence=0.8, perspective="app",
    )
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.9),
    ])
    refs = _algorithmic_refutations(h, store)
    assert any("NOT observed" in r for r in refs)


def test_algo_flags_low_confidence_anchor(facts_weak_oom):
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    refs = _algorithmic_refutations(h, facts_weak_oom)
    assert any("low confidence" in r for r in refs)


def test_algo_silent_on_strong_anchor(facts_oom_observed):
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    refs = _algorithmic_refutations(h, facts_oom_observed)
    assert refs == []


# ---------- _parse_refutations ------------------------------------------

def test_parse_refutations_valid():
    raw = '{"refutations":["recent_deploy is not observed","missing oom evidence"]}'
    assert _parse_refutations(raw) == [
        "recent_deploy is not observed", "missing oom evidence"
    ]


def test_parse_refutations_empty_list():
    assert _parse_refutations('{"refutations":[]}') == []


def test_parse_refutations_invalid_json():
    assert _parse_refutations("garbage") == []


def test_parse_refutations_markdown_fence():
    raw = '```json\n{"refutations":["x"]}\n```'
    assert _parse_refutations(raw) == ["x"]


# ---------- FactCriticAgent ---------------------------------------------

@pytest.mark.asyncio
async def test_critic_short_circuits_on_algo_refutation(facts_weak_oom):
    """LLM не должна дёргаться, если algo уже нашло проблему."""
    called = {"yes": False}

    async def fake_ask(self, *a, **kw):
        called["yes"] = True
        return '{"refutations":[]}'

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        h = Hypothesis(
            cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.9, perspective="infra",
        )
        out = await FactCriticAgent().critique(h, facts_weak_oom)

    assert called["yes"] is False  # LLM пропущена
    assert out.refutations  # algo нашло слабую confidence
    assert "low confidence" in out.refutations[0]


@pytest.mark.asyncio
async def test_critic_uses_llm_when_algo_silent(facts_oom_observed):
    captured = {}

    async def fake_ask(self, user_context, instruction=""):
        captured["called"] = True
        return '{"refutations":["the deploy fact is ✗ so the deploy story fails"]}'

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        h = Hypothesis(
            cause="deploy regression",
            anchored_facts=[FactKind.OOM_KILLED],  # algo молчит
            confidence=0.7, perspective="app",
        )
        out = await FactCriticAgent().critique(h, facts_oom_observed)

    assert captured.get("called") is True
    assert len(out.refutations) == 1
    assert "deploy" in out.refutations[0]


@pytest.mark.asyncio
async def test_critic_survives_llm_failure(facts_oom_observed):
    """LLM упал — критик возвращает hypothesis без refutations (не fail-открытый)."""
    async def boom(self, *a, **kw):
        raise RuntimeError("LLM 500")

    with patch("app.agents.base.BaseAgent.ask", new=boom):
        h = Hypothesis(
            cause="x", anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.8, perspective="infra",
        )
        out = await FactCriticAgent().critique(h, facts_oom_observed)
    assert out.refutations == []  # не ставим ложных refutations при провале LLM


@pytest.mark.asyncio
async def test_critique_all_processes_all_hypotheses(facts_oom_observed):
    async def fake_ask(self, user_context, instruction=""):
        # один с refutation, один без
        if "deploy regression" in user_context:
            return '{"refutations":["recent_deploy is ✗"]}'
        return '{"refutations":[]}'

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        hs = HypothesisSet(items=[
            Hypothesis(cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
                       confidence=0.9, perspective="infra"),
            Hypothesis(cause="deploy regression",
                       anchored_facts=[FactKind.OOM_KILLED],
                       confidence=0.7, perspective="app"),
        ])
        out = await FactCriticAgent().critique_all(hs, facts_oom_observed)

    # survivors / refuted разруливаются хелперами.
    surv = survivors(out)
    ref = refuted(out)
    assert len(surv.items) == 1 and surv.items[0].cause == "OOM"
    assert len(ref.items) == 1 and ref.items[0].cause == "deploy regression"


# ---------- best_candidate ----------------------------------------------

def test_best_candidate_picks_highest_confidence():
    s = HypothesisSet(items=[
        Hypothesis(cause="lo", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.6, perspective="infra"),
        Hypothesis(cause="hi", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="app"),
    ])
    assert best_candidate(s).cause == "hi"


def test_best_candidate_breaks_tie_by_anchor_count():
    s = HypothesisSet(items=[
        Hypothesis(cause="one_anchor", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.8, perspective="infra"),
        Hypothesis(
            cause="two_anchors",
            anchored_facts=[FactKind.OOM_KILLED, FactKind.RESOURCE_PRESSURE],
            confidence=0.8, perspective="infra"),
    ])
    assert best_candidate(s).cause == "two_anchors"


def test_best_candidate_ignores_refuted():
    s = HypothesisSet(items=[
        Hypothesis(cause="alive", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.7, perspective="infra"),
        Hypothesis(cause="dead", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.95, perspective="app",
                   refutations=["killed by critic"]),
    ])
    assert best_candidate(s).cause == "alive"


def test_best_candidate_none_when_all_refuted():
    s = HypothesisSet(items=[
        Hypothesis(cause="x", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra",
                   refutations=["nope"]),
    ])
    assert best_candidate(s) is None
