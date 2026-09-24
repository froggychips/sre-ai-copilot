"""FactCritic в пакетном режиме (FACT_CRITIC_MODE=batch).

Главные инварианты:
  * один LLM-вызов на все гипотезы, пережившие algo-проверку;
  * гипотеза без вердикта в ответе НЕ выживает (fail-closed);
  * algo-refutation и soft down-weight — те же, что в per_hypothesis;
  * порядок гипотез в результате — исходный.
"""
import json
import re
from unittest.mock import patch

import pytest

from app.agents.fact_critic import (BATCH_NO_VERDICT, BATCH_NOT_REVIEWED,
                                    _LOW_CONFIDENCE_PENALTY, FactCriticAgent,
                                    _parse_batch, best_candidate, survivors)
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.config import settings
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.services.llm_service import LLMTruncatedResponse


@pytest.fixture(autouse=True)
def batch_mode(monkeypatch):
    monkeypatch.setattr(settings, "FACT_CRITIC_MODE", "batch")
    monkeypatch.setattr(settings, "FACT_CRITIC_BATCH_TOP_N", 0)
    monkeypatch.setattr(settings, "FACT_CRITIC_BATCH_MAX", 12)


@pytest.fixture
def facts():
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.9),
        Fact(kind=FactKind.PROCESS_CRASH, observed=True, confidence=0.4),  # soft
    ])


def _h(cause, anchor=FactKind.OOM_KILLED, conf=0.8, perspective="infra"):
    return Hypothesis(cause=cause, anchored_facts=[anchor],
                      confidence=conf, perspective=perspective)


def _ids_in(user_context):
    return re.findall(r'<hypothesis id="(h\d+)">', user_context)


def _answer(mapping):
    return json.dumps({"critiques": [
        {"id": k, "refutations": v} for k, v in mapping.items()
    ]})


@pytest.mark.asyncio
async def test_one_call_for_all_hypotheses(facts):
    calls = []

    async def fake_ask(self, user_context, instruction=""):
        calls.append(user_context)
        ids = _ids_in(user_context)
        return _answer({ids[0]: [], ids[1]: ["recent_deploy is ✗"], ids[2]: []})

    hs = HypothesisSet(items=[_h("OOM"), _h("deploy"), _h("leak")])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert len(calls) == 1
    # факты — один раз на пакет
    assert calls[0].count("oom_killed") >= 1
    assert [h.cause for h in out.items] == ["OOM", "deploy", "leak"]
    assert out.items[1].refutations == ["recent_deploy is ✗"]
    assert [h.cause for h in survivors(out).items] == ["OOM", "leak"]


@pytest.mark.asyncio
async def test_missing_id_is_fail_closed(facts):
    async def fake_ask(self, user_context, instruction=""):
        ids = _ids_in(user_context)
        return _answer({ids[0]: []})  # второй id модель «забыла»

    hs = HypothesisSet(items=[_h("OOM"), _h("leak")])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert out.items[0].refutations == []
    assert out.items[1].refutations == [BATCH_NO_VERDICT]
    assert [h.cause for h in survivors(out).items] == ["OOM"]


@pytest.mark.asyncio
async def test_unparseable_response_refutes_every_pending(facts):
    async def fake_ask(self, user_context, instruction=""):
        return "sure, both look fine"

    hs = HypothesisSet(items=[_h("OOM"), _h("leak")])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert all(h.refutations == [BATCH_NO_VERDICT] for h in out.items)
    assert best_candidate(out) is None


@pytest.mark.asyncio
async def test_algo_refuted_never_reach_llm(facts):
    seen = []

    async def fake_ask(self, user_context, instruction=""):
        seen.extend(_ids_in(user_context))
        return _answer({i: [] for i in _ids_in(user_context)})

    hs = HypothesisSet(items=[
        _h("deploy", anchor=FactKind.RECENT_DEPLOY),  # ✗ → algo
        _h("OOM"),
    ])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert seen == ["h1"]  # только одна гипотеза ушла в модель
    assert "NOT observed" in out.items[0].refutations[0]
    assert out.items[1].refutations == []


@pytest.mark.asyncio
async def test_no_llm_call_when_everything_algo_refuted(facts):
    async def fake_ask(self, *a, **kw):
        raise AssertionError("LLM не должна вызываться")

    hs = HypothesisSet(items=[_h("deploy", anchor=FactKind.RECENT_DEPLOY)])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)
    assert out.items[0].refutations


@pytest.mark.asyncio
async def test_soft_downweight_is_kept(facts):
    async def fake_ask(self, user_context, instruction=""):
        return _answer({i: [] for i in _ids_in(user_context)})

    hs = HypothesisSet(items=[_h("crash", anchor=FactKind.PROCESS_CRASH, conf=0.7)])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)
    assert out.items[0].refutations == []
    assert out.items[0].confidence == pytest.approx(0.7 * _LOW_CONFIDENCE_PENALTY)


@pytest.mark.asyncio
async def test_top_n_marks_rest_not_reviewed(facts, monkeypatch):
    monkeypatch.setattr(settings, "FACT_CRITIC_BATCH_TOP_N", 2)
    seen = []

    async def fake_ask(self, user_context, instruction=""):
        seen.append(user_context)
        return _answer({i: [] for i in _ids_in(user_context)})

    hs = HypothesisSet(items=[_h("low", conf=0.3), _h("high", conf=0.9), _h("mid", conf=0.6)])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert len(_ids_in(seen[0])) == 2
    assert "cause: low" not in seen[0]
    by = {h.cause: h for h in out.items}
    assert by["low"].refutations == [BATCH_NOT_REVIEWED]
    assert by["high"].refutations == [] and by["mid"].refutations == []
    assert [h.cause for h in out.items] == ["low", "high", "mid"]  # порядок исходный


@pytest.mark.asyncio
async def test_batch_max_splits_into_calls(facts, monkeypatch):
    monkeypatch.setattr(settings, "FACT_CRITIC_BATCH_MAX", 2)
    calls = []

    async def fake_ask(self, user_context, instruction=""):
        calls.append(_ids_in(user_context))
        return _answer({i: [] for i in _ids_in(user_context)})

    hs = HypothesisSet(items=[_h(f"c{n}") for n in range(5)])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)

    assert [len(c) for c in calls] == [2, 2, 1]
    assert all(h.refutations == [] for h in out.items)


@pytest.mark.asyncio
async def test_truncation_splits_then_raises_on_single(facts):
    sizes = []

    async def fake_ask(self, user_context, instruction=""):
        ids = _ids_in(user_context)
        sizes.append(len(ids))
        if len(ids) > 1:
            raise LLMTruncatedResponse("cut")
        return _answer({ids[0]: []})

    hs = HypothesisSet(items=[_h("a"), _h("b"), _h("c")])
    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        out = await FactCriticAgent().critique_all(hs, facts)
    assert sizes[0] == 3 and all(h.refutations == [] for h in out.items)

    async def always_cut(self, *a, **kw):
        raise LLMTruncatedResponse("cut")

    with patch("app.agents.base.BaseAgent.ask", new=always_cut):
        with pytest.raises(LLMTruncatedResponse):
            await FactCriticAgent().critique_all(HypothesisSet(items=[_h("a")]), facts)


@pytest.mark.asyncio
async def test_batch_failure_splits_before_giving_up(facts):
    sizes = []

    async def flaky(self, user_context, instruction=""):
        ids = _ids_in(user_context)
        sizes.append(len(ids))
        if len(ids) > 1:
            raise RuntimeError("claude CLI timed out after 180.0s")
        return _answer({ids[0]: ["recent_deploy is ✗"]})

    hs = HypothesisSet(items=[_h("a"), _h("b"), _h("c")])
    with patch("app.agents.base.BaseAgent.ask", new=flaky):
        out = await FactCriticAgent().critique_all(hs, facts)
    assert sizes == [3, 1, 2, 1, 1]
    assert all(h.refutations == ["recent_deploy is ✗"] for h in out.items)


@pytest.mark.asyncio
async def test_llm_failure_keeps_parity_with_per_hypothesis(facts):
    async def boom(self, *a, **kw):
        raise RuntimeError("LLM 500")

    hs = HypothesisSet(items=[_h("OOM")])
    with patch("app.agents.base.BaseAgent.ask", new=boom):
        out = await FactCriticAgent().critique_all(hs, facts)
    assert out.items[0].refutations == []


@pytest.mark.asyncio
async def test_batch_uses_own_role(facts):
    """Своя роль — отдельный ключ replay-записей, не ответы per_hypothesis."""
    roles = []

    async def fake_ask(self, user_context, instruction=""):
        roles.append((self.name, self.role))
        return _answer({i: [] for i in _ids_in(user_context)})

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        await FactCriticAgent().critique_all(HypothesisSet(items=[_h("OOM")]), facts)
    assert roles[0][0] == "FactCriticBatch"
    assert roles[0][1] != FactCriticAgent().role


def test_parse_batch_ignores_foreign_and_duplicate_ids():
    raw = _answer({"h1": ["a"], "h9": ["x"]})
    raw = raw[:-2] + ', {"id": "h1", "refutations": []}]}'
    assert _parse_batch(raw, ["h1", "h2"]) == {"h1": ["a"]}


def test_parse_batch_rejects_non_list_refutations():
    raw = json.dumps({"critiques": [{"id": "h1", "refutations": "none"}]})
    assert _parse_batch(raw, ["h1"]) == {}


def test_parse_batch_strips_fences():
    raw = "```json\n" + _answer({"h1": []}) + "\n```"
    assert _parse_batch(raw, ["h1"]) == {"h1": []}


def test_invalid_mode_rejected():
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(FACT_CRITIC_MODE="Batch", LLM_BACKEND="claude_cli")
