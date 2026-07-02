"""Тесты на fact-based adversarial critic."""
from unittest.mock import patch

import pytest

from app.agents.fact_critic import (FactCriticAgent,
                                    _algorithmic_confidence_penalty,
                                    _algorithmic_refutations,
                                    _LOW_CONFIDENCE_PENALTY,
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
    # confidence < _VERY_LOW_CONFIDENCE (0.25) — пренебрежимо слабый сигнал,
    # он ДОЛЖЕН опровергаться алгоритмически (жёсткий algo-refutation).
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.2),  # слабый
    ])


@pytest.fixture
def facts_soft_oom():
    # confidence в soft-зоне [0.25, 0.5) — легитимный soft-OOM (exit 137 only,
    # см. app/diagnostics/rules/oom.py). НЕ должен опровергаться: только
    # down-weight, чтобы единственный слабый anchor не убивал гипотезу.
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.4),
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


def test_algo_does_not_refute_soft_zone_anchor(facts_soft_oom):
    """Факт в soft-зоне [0.25, 0.5) (напр. soft-OOM=0.40) НЕ опровергается
    алгоритмически — раньше жёсткий порог <0.5 его убивал."""
    h = Hypothesis(
        cause="OOM (exit 137 only)", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.7, perspective="infra",
    )
    refs = _algorithmic_refutations(h, facts_soft_oom)
    assert refs == []


def test_algo_still_refutes_negligible_anchor(facts_weak_oom):
    """< _VERY_LOW_CONFIDENCE (0.25) — пренебрежимо слабый, всё ещё refuted."""
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    refs = _algorithmic_refutations(h, facts_weak_oom)
    assert any("low confidence" in r for r in refs)


# ---------- _algorithmic_confidence_penalty -----------------------------

def test_penalty_applied_in_soft_zone(facts_soft_oom):
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.7, perspective="infra",
    )
    assert _algorithmic_confidence_penalty(h, facts_soft_oom) == _LOW_CONFIDENCE_PENALTY


def test_penalty_absent_for_strong_anchor(facts_oom_observed):
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    assert _algorithmic_confidence_penalty(h, facts_oom_observed) == 1.0


def test_penalty_absent_for_negligible_anchor(facts_weak_oom):
    """< 0.25 обрабатывается refutation-ом, а не down-weight-ом → штрафа нет."""
    h = Hypothesis(
        cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
        confidence=0.9, perspective="infra",
    )
    assert _algorithmic_confidence_penalty(h, facts_weak_oom) == 1.0


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


@pytest.mark.asyncio
async def test_soft_zone_anchor_survives_as_best_candidate(facts_soft_oom):
    """Единственный anchor в soft-зоне (0.40) НЕ должен убивать гипотезу.

    Регрессия: раньше жёсткий порог <0.5 ставил algo-refutation → survivors
    пуст → best_candidate=None → TRIAGE_REQUIRED без фикса. Теперь гипотеза
    доживает (down-weighted) и остаётся best_candidate."""
    async def fake_ask(self, user_context, instruction=""):
        return '{"refutations":[]}'  # LLM честно не нашла противоречий

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        h = Hypothesis(
            cause="soft OOM (exit 137 only)",
            anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.7, perspective="infra",
        )
        out = await FactCriticAgent().critique(h, facts_soft_oom)

    assert out.refutations == []            # не опровергнута
    assert out.confidence < 0.7             # но down-weighted
    assert out.confidence == pytest.approx(0.7 * _LOW_CONFIDENCE_PENALTY)

    s = HypothesisSet(items=[out])
    assert best_candidate(s) is not None     # НЕ None — движок не «пожал плечами»
    assert best_candidate(s).cause == "soft OOM (exit 137 only)"


@pytest.mark.asyncio
async def test_soft_zone_ranks_below_strong_competitor():
    """Down-weighted слабая гипотеза уступает уверенному конкуренту, но выживает."""
    facts = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.4),   # soft
        Fact(kind=FactKind.RECENT_DEPLOY, observed=True, confidence=0.95),  # strong
    ])

    async def fake_ask(self, user_context, instruction=""):
        return '{"refutations":[]}'

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        weak = Hypothesis(cause="soft OOM", anchored_facts=[FactKind.OOM_KILLED],
                          confidence=0.7, perspective="infra")
        strong = Hypothesis(cause="deploy regression",
                            anchored_facts=[FactKind.RECENT_DEPLOY],
                            confidence=0.7, perspective="app")
        out = await FactCriticAgent().critique_all(
            HypothesisSet(items=[weak, strong]), facts
        )

    surv = survivors(out)
    assert len(surv.items) == 2               # обе выжили
    assert best_candidate(out).cause == "deploy regression"  # сильная сверху


@pytest.mark.asyncio
async def test_soft_zone_downweight_survives_llm_failure(facts_soft_oom):
    """Даже если LLM упала, down-weight сохраняется, а refutations остаются пусты."""
    async def boom(self, *a, **kw):
        raise RuntimeError("LLM 500")

    with patch("app.agents.base.BaseAgent.ask", new=boom):
        h = Hypothesis(
            cause="soft OOM", anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.7, perspective="infra",
        )
        out = await FactCriticAgent().critique(h, facts_soft_oom)

    assert out.refutations == []
    assert out.confidence == pytest.approx(0.7 * _LOW_CONFIDENCE_PENALTY)


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
