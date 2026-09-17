"""Предохранитель бюджета на пути реального вызова модели.

Отдельно от `test_cost_guard.py`: там проверяется механика резерва, здесь —
что резерв кто-то берёт, а сводит его тот же код, который получает ответ.
Модуль без этого выглядел бы защитой, не будучи ею.
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


def _reserved(amount=1.0):
    return BudgetVerdict(True, "within_budget", 1.0, 10.0, reserved_usd=amount)


def _denied():
    return BudgetVerdict(False, "daily_budget_exhausted", 10.0, 10.0)


@pytest.mark.asyncio
async def test_exhausted_budget_blocks_before_the_call(priced):
    """Отказ происходит ДО обращения к модели, а не после."""
    router = AsyncMock()
    with patch("app.agents.base.reserve", return_value=_denied()), \
         patch("app.agents.base.ModelRouter.route_and_call_full", new=router):
        with pytest.raises(LLMBudgetExceeded):
            await BaseAgent(name="A", role="r").ask("контекст")

    router.assert_not_awaited(), "модель не должна вызываться при исчерпанном бюджете"


@pytest.mark.asyncio
async def test_reservation_is_taken_on_the_full_prompt(priced):
    """Резервируется стоимость ГОТОВОГО промпта, а не голого контекста.

    Роль и инструкция уезжают в модель вместе с контекстом и стоят денег;
    оценка по одному `user_context` систематически занижала бы резерв.
    """
    result = {"text": "ответ", "input_tokens": 10, "output_tokens": 5, "model": "m"}
    with patch("app.agents.base.reserve", return_value=_reserved()) as reserve, \
         patch("app.agents.base.settle", return_value=0.1), \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        await BaseAgent(name="A", role="РОЛЬ-МАРКЕР").ask("контекст", instruction="ЗАДАЧА-МАРКЕР")

    prompt = reserve.call_args.args[1]
    assert "РОЛЬ-МАРКЕР" in prompt and "ЗАДАЧА-МАРКЕР" in prompt
    assert "контекст" in prompt


@pytest.mark.asyncio
async def test_successful_call_settles_the_reserve(priced):
    result = {"text": "ответ", "input_tokens": 1_000_000, "output_tokens": 0, "model": "m"}
    with patch("app.agents.base.reserve", return_value=_reserved()), \
         patch("app.agents.base.settle", return_value=3.0) as settle, \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        await BaseAgent(name="A", role="r").ask("контекст")

    settle.assert_called_once()
    assert settle.call_args.args[2] == 1_000_000


@pytest.mark.asyncio
async def test_provider_error_keeps_the_reserve_charged(priced):
    """Провайдер упал — резерв остаётся списанным, и это правильно.

    `LLMService.generate_full` поднимает ValueError на пустом ответе раньше,
    чем usage доедет до учёта: токены оплачены, а чисел о них нет. Вернуть
    резерв значило бы открыть потолок ровно на неудачных прогонах, которых
    при проблемах с моделью больше всего.
    """
    released = []
    with patch("app.agents.base.reserve", return_value=_reserved(2.5)), \
         patch("app.agents.base.settle", side_effect=lambda *a, **k: released.append(a)), \
         patch(
             "app.agents.base.ModelRouter.route_and_call_full",
             side_effect=ValueError("Empty response from LLM"),
         ):
        with pytest.raises(ValueError):
            await BaseAgent(name="A", role="r").ask("контекст")

    assert not released, "сводить нечего: ответа не было, резерв остаётся списанным"


@pytest.mark.asyncio
async def test_truncated_json_response_is_still_settled(priced):
    """Обрезанный ответ оплачен: вызов состоялся, usage известен."""
    result = {
        "text": '{"refutations": [',
        "input_tokens": 400_000,
        "output_tokens": 100_000,
        "model": "m",
        "truncated": True,
    }
    from app.services.llm_service import LLMTruncatedResponse

    with patch("app.agents.base.reserve", return_value=_reserved()), \
         patch("app.agents.base.settle", return_value=2.7) as settle, \
         patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        with pytest.raises(LLMTruncatedResponse):
            await BaseAgent(name="A", role="r", json_response=True).ask("контекст")

    settle.assert_called_once()


@pytest.mark.asyncio
async def test_budget_exception_is_not_retriable():
    """Ретрай при исчерпанном потолке — это попытка потратить ×3."""
    from app.workers.tasks import RETRIABLE_EXC

    assert not issubclass(LLMBudgetExceeded, RETRIABLE_EXC)
