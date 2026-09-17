"""Предохранитель бюджета на пути реального вызова модели.

Отдельно от `test_cost_guard.py`: там проверяется арифметика и вердикт,
здесь — что вердикт кто-то спрашивает, а трата кем-то списывается. Первое
без второго — мёртвый модуль, который выглядит как защита.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base import BaseAgent
from app.config import settings
from app.services.cost_guard import BudgetVerdict, LLMBudgetExceeded


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setattr(
        settings, "LLM_PRICE_PER_MTOK",
        {"m": {"input": 3.0, "output": 15.0}}, raising=False,
    )
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)


def _allowed():
    return BudgetVerdict(allowed=True, reason="within_budget", spent_usd=1.0, limit_usd=10.0)


def _denied():
    return BudgetVerdict(
        allowed=False, reason="daily_budget_exhausted", spent_usd=10.0, limit_usd=10.0
    )


@pytest.mark.asyncio
async def test_exhausted_budget_blocks_before_the_call(priced):
    """Отказ происходит ДО обращения к модели, а не после.

    Проверка после вызова превышала бы потолок ровно на стоимость этого
    вызова — у пайплайна из семи агентов это не округление.
    """
    router = AsyncMock()
    with patch("app.agents.base.check_budget", return_value=_denied()), \
         patch("app.agents.base.ModelRouter.route_and_call_full", new=router):
        with pytest.raises(LLMBudgetExceeded):
            await BaseAgent(name="A", role="r").ask("контекст")

    router.assert_not_awaited(), "модель не должна вызываться при исчерпанном бюджете"


@pytest.mark.asyncio
async def test_successful_call_is_charged(priced):
    result = {"text": "ответ", "input_tokens": 1_000_000, "output_tokens": 0, "model": "m"}
    with patch("app.agents.base.check_budget", return_value=_allowed()), \
         patch("app.agents.base.record_spend", return_value=3.0) as spend, \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        await BaseAgent(name="A", role="r").ask("контекст")

    spend.assert_called_once()
    assert spend.call_args.args[1] == 1_000_000


@pytest.mark.asyncio
async def test_empty_response_is_still_charged(priced):
    """Пустой ответ оплачен — списать его обязаны.

    Иначе потолок становится декоративным ровно тогда, когда модель ведёт
    себя плохо: неудачные прогоны не учитываются, а повторов от них больше.
    """
    result = {"text": "", "input_tokens": 500_000, "output_tokens": 0, "model": "m"}
    with patch("app.agents.base.check_budget", return_value=_allowed()), \
         patch("app.agents.base.record_spend", return_value=1.5) as spend, \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        with pytest.raises(ValueError):
            await BaseAgent(name="A", role="r").ask("контекст")

    spend.assert_called_once(), "неудачный ответ тоже стоил денег"


@pytest.mark.asyncio
async def test_truncated_json_response_is_still_charged(priced):
    """То же для обрезанного ответа JSON-агента: вызов состоялся и оплачен."""
    result = {
        "text": '{"refutations": [',
        "input_tokens": 400_000,
        "output_tokens": 100_000,
        "model": "m",
        "truncated": True,
    }
    from app.services.llm_service import LLMTruncatedResponse

    with patch("app.agents.base.check_budget", return_value=_allowed()), \
         patch("app.agents.base.record_spend", return_value=2.7) as spend, \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        with pytest.raises(LLMTruncatedResponse):
            await BaseAgent(name="A", role="r", json_response=True).ask("контекст")

    spend.assert_called_once()


@pytest.mark.asyncio
async def test_budget_exception_is_not_retriable():
    """Ретрай при исчерпанном потолке — это попытка потратить ×3."""
    from app.workers.tasks import RETRIABLE_EXC

    assert not issubclass(LLMBudgetExceeded, RETRIABLE_EXC)
