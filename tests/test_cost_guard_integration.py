"""Предохранитель бюджета на пути реального вызова модели.

Отдельно от `test_cost_guard.py`: там проверяется механика резерва, здесь —
что резерв кто-то берёт, а сводит его тот же код, который получает ответ.
Модуль без этого выглядел бы защитой, не будучи ею.
"""
from unittest.mock import AsyncMock, MagicMock, patch

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


def _anthropic_response():
    """Минимальный ответ Anthropic SDK: текстовый блок + usage."""
    block = MagicMock()
    block.type = "text"
    block.text = "ответ"
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)
    return response


def _denied():
    return BudgetVerdict(False, "daily_budget_exhausted", 10.0, 10.0)


# --- бюджет на уровне ПОПЫТКИ, а не вызова агента -------------------------

@pytest.mark.asyncio
async def test_every_retry_attempt_is_reserved(priced):
    """Главное: резерв берётся на каждое обращение к провайдеру.

    generate_full обёрнут llm_retry_strategy (3 попытки), и таймаут среди
    ретраибельных ошибок означает, что запрос дошёл и мог быть обработан —
    то есть оплачен. Один резерв на три оплачиваемые попытки делал бы
    потолок ненастоящим ровно тогда, когда провайдеру плохо.
    """
    import anthropic

    from app.services import llm_service as svc

    calls = {"reserve": 0, "create": 0}

    def _reserve(*_a, **_k):
        calls["reserve"] += 1
        return _reserved()

    async def _create(*_a, **_k):
        calls["create"] += 1
        if calls["create"] < 3:
            raise anthropic.APITimeoutError(request=MagicMock())
        return _anthropic_response()

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", _reserve), \
         patch.object(svc, "settle", lambda *a, **k: 0.5), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        result = await service.generate_full("привет")

    assert calls["create"] == 3, "ожидались три обращения к провайдеру"
    assert calls["reserve"] == 3, "каждая попытка обязана резервировать"
    assert result["text"] == "ответ"


@pytest.mark.asyncio
async def test_timeout_keeps_the_reservation_charged(priced):
    """Таймаут — попытка могла быть оплачена, резерв не возвращаем."""
    import anthropic

    from app.services import llm_service as svc

    released = []

    async def _create(*_a, **_k):
        raise anthropic.APITimeoutError(request=MagicMock())

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _reserved()), \
         patch.object(svc, "release", lambda *a, **k: released.append(a)), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        with pytest.raises(Exception):
            await service.generate_full("привет")

    assert not released, "резерв таймаута не возвращается: ответ мог быть сгенерирован"


@pytest.mark.asyncio
async def test_rate_limit_returns_the_reservation(priced):
    """429 — провайдер отказал до обработки, платить не за что.

    Без возврата шторм rate-limit'ов съедал бы суточный бюджет отказами,
    за которые никто не выставил счёт.
    """
    import anthropic

    from app.services import llm_service as svc

    released = []

    async def _create(*_a, **_k):
        raise anthropic.RateLimitError(
            "rate limited", response=MagicMock(status_code=429), body=None
        )

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _reserved()), \
         patch.object(svc, "release", lambda *a, **k: released.append(a)), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        with pytest.raises(Exception):
            await service.generate_full("привет")

    assert released, "резерв 429 должен вернуться в бюджет"


@pytest.mark.asyncio
async def test_exhausted_budget_blocks_the_provider_call(priced):
    """Отказ бюджета происходит ДО обращения к провайдеру."""
    from app.services import llm_service as svc

    calls = {"create": 0}

    async def _create(*_a, **_k):
        calls["create"] += 1
        return _anthropic_response()

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _denied()), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        with pytest.raises(LLMBudgetExceeded):
            await service.generate_full("привет")

    assert calls["create"] == 0, "модель не должна вызываться при исчерпанном бюджете"


@pytest.mark.asyncio
async def test_budget_denial_is_not_retried(priced):
    """Ретрай отказа бюджета — повтор попытки потратить запрещённое."""
    from app.services import llm_service as svc
    from app.services.resilience import is_retryable_llm_error

    assert not is_retryable_llm_error(LLMBudgetExceeded("нет денег"))

    calls = {"reserve": 0}

    def _reserve(*_a, **_k):
        calls["reserve"] += 1
        return _denied()

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_response())

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", _reserve), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        with pytest.raises(LLMBudgetExceeded):
            await service.generate_full("привет")

    assert calls["reserve"] == 1, "отказ бюджета не должен уходить в retry-петлю"


@pytest.mark.asyncio
async def test_budget_denial_does_not_open_provider_circuit(priced):
    """Отказ бюджета — наше решение, провайдер ни при чём.

    Уходя в общий обработчик, он репортился бы как сбой anthropic: пять
    отказов подряд открывают circuit, и вызовы остаются заблокированными
    даже после того, как ledger поднялся или сменились сутки.
    """
    from app.services import llm_service as svc

    reports = []

    async def _report(_resilience, success):
        reports.append(success)

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_response())

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _denied()), \
         patch.object(svc, "_report_provider", _report), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=MagicMock()):
        with pytest.raises(LLMBudgetExceeded):
            await service.generate_full("привет")

    assert reports == [], "отказ бюджета не должен считаться сбоем провайдера"


@pytest.mark.asyncio
async def test_metric_counts_retained_reservations(priced):
    """Метрика обязана показывать то же, что списал ledger.

    Резерв неудачной попытки остаётся списанным, и учитывать только
    стоимость последнего успешного ответа значит занижать расход ровно во
    время retry-штормов, когда попыток больше всего.
    """
    import anthropic

    from app.services import llm_service as svc

    costs = []

    async def _create(*_a, **_k):
        if len(costs) < 2:
            raise anthropic.APITimeoutError(request=MagicMock())
        return _anthropic_response()

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _reserved(2.0)), \
         patch.object(svc, "settle", lambda *a, **k: 0.5), \
         patch.object(svc, "track_llm_cost", lambda _m, c: costs.append(c)), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        await service.generate_full("привет")

    # Две удержанные оценки по 2.0 + фактическая стоимость успешной 0.5.
    assert costs == [2.0, 2.0, 0.5]


@pytest.mark.asyncio
async def test_terminal_failure_still_reports_cost(priced):
    """Провал всех попыток тоже стоил денег — метрика не должна молчать."""
    import anthropic

    from app.services import llm_service as svc

    costs = []

    async def _create(*_a, **_k):
        raise anthropic.APITimeoutError(request=MagicMock())

    client = MagicMock()
    client.messages.create = _create

    service = svc.LLMService()
    service.backend = "anthropic"
    service.model = "m"
    with patch.object(svc, "reserve", lambda *a, **k: _reserved(2.0)), \
         patch.object(svc, "track_llm_cost", lambda _m, c: costs.append(c)), \
         patch.object(service, "_anthropic_client", return_value=client), \
         patch.object(svc, "_get_resilience", return_value=None):
        with pytest.raises(Exception):
            await service.generate_full("привет")

    assert costs, "терминальный провал не должен давать нулевой расход"


@pytest.mark.asyncio
async def test_agent_does_not_double_count_cost(priced):
    """Учёт живёт в одном месте: агент не инкрементит поверх."""
    result = {
        "text": "ответ", "input_tokens": 10, "output_tokens": 5,
        "model": "m", "cost_usd": 0.42,
    }
    with patch("app.agents.base.ModelRouter.route_and_call_full", return_value=result):
        import app.agents.base as base

        assert not hasattr(base, "track_llm_cost"), (
            "агент не должен писать стоимость — иначе успешная попытка "
            "попадёт в счётчик дважды"
        )
        await BaseAgent(name="A", role="r").ask("контекст")


@pytest.mark.asyncio
async def test_budget_exception_is_not_retriable_by_celery():
    """Celery тоже не должен ретраить исчерпанный потолок."""
    from app.workers.tasks import RETRIABLE_EXC

    assert not issubclass(LLMBudgetExceeded, RETRIABLE_EXC)
