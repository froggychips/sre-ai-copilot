"""FixAgent теперь возвращает (raw_text, Optional[ExecutionIntent]).

Проверяем что:
  - happy path: LLM выдал валидный JSON → intent распарсен.
  - bad JSON / prose: intent=None, raw возвращается как есть (advisory-fallback).
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.fix import FixAgent
from app.core.execution_dsl import ActionType


@pytest.mark.asyncio
async def test_suggest_returns_intent_on_valid_json():
    """LLM выдаёт чистый JSON по нашему prompt-у → from_llm_response парсит."""
    fake_response = (
        '{"action": "restart_deployment", "resource_type": "deployment", '
        '"resource_name": "town-service", "namespace": "squad-1", '
        '"params": {}, "risk": "low"}'
    )
    with patch(
        "app.agents.base.BaseAgent.ask",
        new=AsyncMock(return_value=fake_response),
    ):
        raw, intent = await FixAgent().suggest("OOMKilled in town-service")

    assert raw == fake_response
    assert intent is not None
    assert intent.action == ActionType.RESTART_DEPLOYMENT
    assert intent.resource_name == "town-service"


@pytest.mark.asyncio
async def test_suggest_returns_none_intent_on_prose():
    """Если LLM игнорирует JSON-инструкцию — intent=None, raw остаётся."""
    fake_response = "I recommend restarting the deployment manually."
    with patch(
        "app.agents.base.BaseAgent.ask",
        new=AsyncMock(return_value=fake_response),
    ):
        raw, intent = await FixAgent().suggest("OOMKilled")

    assert raw == fake_response
    assert intent is None


@pytest.mark.asyncio
async def test_suggest_returns_none_intent_on_forbidden_namespace():
    """LLM проложил действие в kube-system → intent=None (advisory-fallback)."""
    fake_response = (
        '{"action": "restart_deployment", "resource_type": "deployment", '
        '"resource_name": "coredns", "namespace": "kube-system", '
        '"params": {}, "risk": "high"}'
    )
    with patch(
        "app.agents.base.BaseAgent.ask",
        new=AsyncMock(return_value=fake_response),
    ):
        raw, intent = await FixAgent().suggest("DNS pod down")

    assert raw == fake_response
    assert intent is None


@pytest.mark.asyncio
async def test_suggest_recurrence_mode_still_returns_tuple():
    """is_recurrence=True добавляет prefix к инструкции, но контракт тот же."""
    fake_response = (
        '{"action": "get_logs", "resource_type": "pod", '
        '"resource_name": "town-abc", "namespace": "squad-1"}'
    )
    with patch(
        "app.agents.base.BaseAgent.ask",
        new=AsyncMock(return_value=fake_response),
    ):
        raw, intent = await FixAgent().suggest(
            "Recurring OOMKilled in town-service",
            is_recurrence=True,
        )

    assert raw == fake_response
    assert intent is not None
    assert intent.action == ActionType.GET_LOGS
