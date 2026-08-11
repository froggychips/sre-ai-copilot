"""Проброс truncated (stop_reason='max_tokens') от generate_full до JSON-агентов.

Инцидентный контекст (кодревью 2026-08): generate_full честно вычислял
`truncated=True`, но BaseAgent.ask возвращал ТОЛЬКО text — флаг терялся.
Последствия при MAX_TOKENS=1024 (дефолт config.py):
  * обрезанный JSON FactCritic → _parse_refutations вернёт [] → слабая
    гипотеза «переживает» критику с ПОЛНОЙ confidence → уверенно-неверный
    root cause уходит людям в Discord/Jira;
  * обрезанный JSON perspective-агента → [] гипотез, неотличимо от честного
    «нечего предложить».

Контракт после фикса:
  * BaseAgent(json_response=True) при truncated → LLMTruncatedResponse;
  * FactCriticAgent / PerspectiveAgent — json_response=True, обрезка = фейл
    стадии (пайплайн обработает своей error-обработкой), а НЕ пустой список;
  * прозаические агенты (Analyzer/Synthesizer, json_response=False) обрезку
    переживают: warning + частичный текст;
  * LLMTruncatedResponse НЕ ретраибелен ни на уровне LLM-вызова
    (is_retryable_llm_error), ни на уровне Celery (RETRIABLE_EXC) — повтор
    того же промпта с тем же MAX_TOKENS обрежется снова.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.base import BaseAgent
from app.agents.fact_critic import FactCriticAgent
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.agents.multi_hypothesis import MultiHypothesisAgent
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.services.llm_service import LLMService, LLMTruncatedResponse


# ── Хелперы: реальный generate_full поверх мокнутого anthropic-клиента ───────
# Мокаем на границе SDK (как tests/test_llm_token_tracking.py), а не сам
# generate_full — чтобы в тесте участвовала НАСТОЯЩАЯ логика вычисления
# truncated из stop_reason.

def _svc_with_stop_reason(text: str, stop_reason: str) -> LLMService:
    svc = LLMService()
    # Backend пинится ЯВНО: stop_reason (а значит и truncated) существует
    # только у anthropic-SDK — claude_cli через `--print` его не отдаёт и
    # всегда сообщает truncated=False. CI гоняет с LLM_BACKEND=claude_cli
    # (см. .github/workflows/ci.yml), поэтому без этой строки мок SDK не
    # участвовал и тест уходил в реальный subprocess.
    svc.backend = "anthropic"
    svc.client = MagicMock()
    svc.client.messages.create = AsyncMock(
        return_value=MagicMock(
            content=[MagicMock(type="text", text=text)],
            usage=MagicMock(input_tokens=100, output_tokens=1024),
            stop_reason=stop_reason,
        )
    )
    return svc


def _route_through(svc: LLMService):
    """patch-объект для ModelRouter.route_and_call_full → svc.generate_full."""
    async def _route(task_type, prompt):
        return await svc.generate_full(prompt)

    return AsyncMock(side_effect=_route)


@pytest.fixture
def no_resilience():
    """Circuit breaker не участвует: тест не должен зависеть от живого Redis."""
    with patch("app.services.llm_service._get_resilience", return_value=None):
        yield


@pytest.fixture
def facts_oom_observed():
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.9),
    ])


# ── Базовый инвариант: generate_full действительно помечает обрезку ──────────

@pytest.mark.asyncio
async def test_generate_full_marks_truncated(no_resilience):
    svc = _svc_with_stop_reason('{"refutations": [', "max_tokens")
    result = await svc.generate_full("prompt")
    assert result["truncated"] is True
    assert result["stop_reason"] == "max_tokens"


# ── (а) FactCritic: обрезка = ошибка стадии, а не пустые refutations ─────────

@pytest.mark.asyncio
async def test_fact_critic_raises_stage_error_on_truncation(
    no_resilience, facts_oom_observed
):
    """Обрезанный JSON критика больше НЕ превращается в «противоречий нет»."""
    svc = _svc_with_stop_reason(
        '{"refutations": ["anchor recent_deploy is NOT obser',  # хвост потерян
        "max_tokens",
    )
    h = Hypothesis(
        cause="deploy regression",
        anchored_facts=[FactKind.OOM_KILLED],  # algo молчит → идём в LLM
        confidence=0.9, perspective="app",
    )
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full", new=_route_through(svc)
    ):
        with pytest.raises(LLMTruncatedResponse):
            await FactCriticAgent().critique(h, facts_oom_observed)


@pytest.mark.asyncio
async def test_fact_critic_truncation_fails_whole_critique_all(
    no_resilience, facts_oom_observed
):
    """critique_all пробрасывает наружу — stage_critique падает целиком.

    Без этого гипотеза выходила бы из критики нетронутой (refutations=[]) и
    попадала в best_candidate с полной confidence.
    """
    svc = _svc_with_stop_reason('{"refutations": [', "max_tokens")
    hs = HypothesisSet(items=[
        Hypothesis(cause="OOM", anchored_facts=[FactKind.OOM_KILLED],
                   confidence=0.9, perspective="infra"),
    ])
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full", new=_route_through(svc)
    ):
        with pytest.raises(LLMTruncatedResponse):
            await FactCriticAgent().critique_all(hs, facts_oom_observed)


@pytest.mark.asyncio
async def test_fact_critic_non_truncated_llm_failure_still_degrades(
    facts_oom_observed
):
    """Регресс-гвард: обычный сбой LLM по-прежнему НЕ роняет стадию."""
    async def boom(self, *a, **kw):
        raise RuntimeError("LLM 500")

    with patch("app.agents.base.BaseAgent.ask", new=boom):
        h = Hypothesis(
            cause="x", anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.8, perspective="infra",
        )
        out = await FactCriticAgent().critique(h, facts_oom_observed)
    assert out.refutations == []


# ── (б) MultiHypothesis: обрезка = ошибка стадии, а не [] гипотез ────────────

@pytest.mark.asyncio
async def test_multi_hypothesis_raises_stage_error_on_truncation(no_resilience):
    """Обрезка у perspective-агента больше НЕ превращается в пустой набор.

    Обрезка систематична (один MAX_TOKENS + одна форма промпта на все
    perspective), поэтому per-perspective толерантность здесь означала бы
    молчаливо суженное/пустое пространство гипотез.
    """
    svc = _svc_with_stop_reason('{"hypotheses": [{"cause": "OOM in ku', "max_tokens")
    facts = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full", new=_route_through(svc)
    ):
        with pytest.raises(LLMTruncatedResponse):
            await MultiHypothesisAgent().generate(
                incident_summary="pod stub-1 OOMKilled", facts=facts
            )


@pytest.mark.asyncio
async def test_multi_hypothesis_survives_ordinary_perspective_failure():
    """Регресс-гвард: НЕ-truncated сбой одного perspective по-прежнему терпим."""
    async def flaky_ask(self, user_context, instruction=""):
        if self.name == "Hypothesis-deps":
            raise RuntimeError("LLM provider 500")
        return (
            '{"hypotheses":[{"cause":"ok","anchored_facts":["oom_killed"],'
            '"confidence":0.7}]}'
        )

    facts = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])
    with patch("app.agents.base.BaseAgent.ask", new=flaky_ask):
        out = await MultiHypothesisAgent().generate(
            incident_summary="x", facts=facts
        )
    assert {h.perspective for h in out.items} == {"app", "infra"}


# ── (в) Прозаические агенты: обрезка терпима (warning, не исключение) ────────

@pytest.mark.asyncio
async def test_prose_agent_returns_truncated_text_with_warning(no_resilience):
    """json_response=False → частичный текст полезен, ask его отдаёт."""
    svc = _svc_with_stop_reason("Partial analysis of the incident", "max_tokens")
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full", new=_route_through(svc)
    ), patch("app.agents.base.logger") as mock_logger:
        agent = BaseAgent(name="Analyzer", role="Senior SRE Analyst")
        text = await agent.ask(user_context="ctx", instruction="analyze")

    assert text == "Partial analysis of the incident"
    warned = [c for c in mock_logger.warning.call_args_list
              if c.args and c.args[0] == "llm_response_truncated"]
    assert len(warned) == 1  # видно в логах, но стадия жива


@pytest.mark.asyncio
async def test_prose_agent_unaffected_when_not_truncated(no_resilience):
    svc = _svc_with_stop_reason("Full analysis", "end_turn")
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full", new=_route_through(svc)
    ):
        text = await BaseAgent(name="Analyzer", role="r").ask(user_context="ctx")
    assert text == "Full analysis"


def test_prose_agents_are_not_json_response():
    """Гайдрейл: analyzer/synthesis остаются прозаическими (обратная совместимость)."""
    from app.agents.analyzer import AnalyzerAgent
    from app.agents.synthesis import SynthesisAgent

    assert AnalyzerAgent().json_response is False
    assert SynthesisAgent().json_response is False


def test_json_agents_are_marked():
    from app.agents.multi_hypothesis import PerspectiveAgent

    assert FactCriticAgent().json_response is True
    assert PerspectiveAgent("infra").json_response is True


# ── Ретраибельность: нигде (ни LLM-слой, ни Celery) ─────────────────────────

def test_truncated_is_not_retryable_at_llm_layer():
    """Повтор того же промпта с тем же MAX_TOKENS обрежется детерминированно."""
    from app.services.resilience import is_retryable_llm_error

    assert is_retryable_llm_error(LLMTruncatedResponse("truncated")) is False


def test_truncated_is_not_celery_retriable():
    """Не в RETRIABLE_EXC → tasks.py фиксирует терминальный фейл стадии."""
    from app.workers.tasks import RETRIABLE_EXC

    assert not isinstance(LLMTruncatedResponse("truncated"), RETRIABLE_EXC)
