"""Тесты на real-token tracking (Grok review #2).

Что проверяем:
  - LLMService.generate_full возвращает usage из Anthropic response
  - claude_cli backend → input/output_tokens=0 (subprocess без usage API)
  - BaseAgent.ask пишет per-agent metrics (track_llm_usage_per_agent)
  - audit-event LLM_CALL содержит agent + tokens + duration
  - При ошибке LLM-вызова: LLM_CALL_FAILED audit + error counter
  - generate_content (legacy) всё ещё возвращает только text
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── LLMService.generate_full: anthropic backend ─────────────────────────────

@pytest.mark.asyncio
async def test_generate_full_anthropic_returns_real_usage():
    """Mock Anthropic response с usage → generate_full extracts real numbers."""
    from app.services.llm_service import LLMService

    fake_response = MagicMock(
        content=[MagicMock(type="text", text="generated text")],
        usage=MagicMock(input_tokens=234, output_tokens=89),
    )
    with patch("app.services.llm_service.settings") as mock_settings:
        mock_settings.LLM_BACKEND = "anthropic"
        mock_settings.MODEL_NAME = "claude-sonnet-4-6"
        mock_settings.MAX_TOKENS = 1024
        mock_settings.LLM_TIMEOUT_SECONDS = 30.0
        mock_settings.ANTHROPIC_API_KEY = "test-key"

        svc = LLMService()
        svc.client = MagicMock()
        svc.client.messages.create = AsyncMock(return_value=fake_response)

        result = await svc.generate_full("test prompt")

    assert result["text"] == "generated text"
    assert result["input_tokens"] == 234
    assert result["output_tokens"] == 89
    assert result["model"] == "claude-sonnet-4-6"
    assert result["backend"] == "anthropic"


@pytest.mark.asyncio
async def test_generate_full_anthropic_missing_usage_defaults_to_zero():
    """Если SDK почему-то не вернул usage — defaults 0/0, не падаем."""
    from app.services.llm_service import LLMService

    fake_response = MagicMock(
        content=[MagicMock(type="text", text="ok")],
        usage=None,  # API не вернул usage
    )
    with patch("app.services.llm_service.settings") as mock_settings:
        mock_settings.LLM_BACKEND = "anthropic"
        mock_settings.MODEL_NAME = "x"
        mock_settings.MAX_TOKENS = 1024
        mock_settings.LLM_TIMEOUT_SECONDS = 30.0
        mock_settings.ANTHROPIC_API_KEY = "k"

        svc = LLMService()
        svc.client = MagicMock()
        svc.client.messages.create = AsyncMock(return_value=fake_response)

        result = await svc.generate_full("prompt")

    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


# ── stop_reason / truncation visibility ─────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_full_anthropic_flags_max_tokens_truncation():
    """stop_reason='max_tokens' → truncated=True + warning (обрезка видна)."""
    from app.services.llm_service import LLMService

    fake_response = MagicMock(
        content=[MagicMock(type="text", text='{"partial": ')],
        usage=MagicMock(input_tokens=100, output_tokens=1024),
        stop_reason="max_tokens",
    )
    with patch("app.services.llm_service.settings") as mock_settings, patch(
        "app.services.llm_service.logging.warning"
    ) as mock_warn:
        mock_settings.LLM_BACKEND = "anthropic"
        mock_settings.MODEL_NAME = "claude-sonnet-4-6"
        mock_settings.MAX_TOKENS = 1024
        mock_settings.LLM_TIMEOUT_SECONDS = 30.0
        mock_settings.ANTHROPIC_API_KEY = "k"

        svc = LLMService()
        svc.client = MagicMock()
        svc.client.messages.create = AsyncMock(return_value=fake_response)

        result = await svc.generate_full("prompt")

    assert result["stop_reason"] == "max_tokens"
    assert result["truncated"] is True
    # обрезка должна быть залогирована (не молча)
    mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_generate_full_anthropic_end_turn_not_truncated():
    """stop_reason='end_turn' → truncated=False, без warning."""
    from app.services.llm_service import LLMService

    fake_response = MagicMock(
        content=[MagicMock(type="text", text="complete answer")],
        usage=MagicMock(input_tokens=100, output_tokens=50),
        stop_reason="end_turn",
    )
    with patch("app.services.llm_service.settings") as mock_settings, patch(
        "app.services.llm_service.logging.warning"
    ) as mock_warn:
        mock_settings.LLM_BACKEND = "anthropic"
        mock_settings.MODEL_NAME = "claude-sonnet-4-6"
        mock_settings.MAX_TOKENS = 1024
        mock_settings.LLM_TIMEOUT_SECONDS = 30.0
        mock_settings.ANTHROPIC_API_KEY = "k"

        svc = LLMService()
        svc.client = MagicMock()
        svc.client.messages.create = AsyncMock(return_value=fake_response)

        result = await svc.generate_full("prompt")

    assert result["stop_reason"] == "end_turn"
    assert result["truncated"] is False
    mock_warn.assert_not_called()


# ── claude_cli backend ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_full_cli_backend_returns_zero_usage():
    """CLI-backend (subprocess) → usage недоступен, ожидаем 0/0."""
    from app.services.llm_service import LLMService

    with patch("app.services.llm_service.settings") as mock_settings:
        mock_settings.LLM_BACKEND = "claude_cli"
        mock_settings.MODEL_NAME = "claude-sonnet-4-6"
        mock_settings.CLAUDE_CLI_TIMEOUT_SECONDS = 180.0

        svc = LLMService()
        svc.cli = MagicMock()
        svc.cli.generate_content = AsyncMock(return_value="cli output")

        result = await svc.generate_full("prompt")

    assert result["text"] == "cli output"
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["backend"] == "claude_cli"


# ── Backward-compat: generate_content (text-only) ───────────────────────────

@pytest.mark.asyncio
async def test_generate_content_legacy_returns_text_only():
    from app.services.llm_service import LLMService

    fake_response = MagicMock(
        content=[MagicMock(type="text", text="hello")],
        usage=MagicMock(input_tokens=10, output_tokens=2),
    )
    with patch("app.services.llm_service.settings") as mock_settings:
        mock_settings.LLM_BACKEND = "anthropic"
        mock_settings.MODEL_NAME = "x"
        mock_settings.MAX_TOKENS = 1024
        mock_settings.LLM_TIMEOUT_SECONDS = 30.0
        mock_settings.ANTHROPIC_API_KEY = "k"

        svc = LLMService()
        svc.client = MagicMock()
        svc.client.messages.create = AsyncMock(return_value=fake_response)

        text = await svc.generate_content("prompt")
    assert text == "hello"  # str, не dict


# ── BaseAgent.ask: real tokens → per-agent metrics + audit ─────────────────

@pytest.mark.asyncio
async def test_base_agent_records_per_agent_usage_and_audit():
    """BaseAgent.ask → ModelRouter.route_and_call_full → tracking helpers."""
    from app.agents.base import BaseAgent

    fake_result = {
        "text": "ok",
        "input_tokens": 500,
        "output_tokens": 150,
        "model": "claude-sonnet-4-6",
        "backend": "anthropic",
    }
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.base.track_llm_usage_per_agent"
    ) as mock_track, patch(
        "app.agents.base.audit_service.log_event"
    ) as mock_audit, patch(
        "app.agents.base.record_llm_call"
    ):
        agent = BaseAgent(name="Fixer", role="K8s expert")
        result = await agent.ask(user_context="ctx", instruction="do x")

    assert result == "ok"
    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    assert kwargs["agent"] == "Fixer"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["input_tokens"] == 500
    assert kwargs["output_tokens"] == 150
    assert kwargs.get("error_type") is None

    # audit event LLM_CALL с правильными полями
    audit_calls = [c for c in mock_audit.call_args_list if c.args[0] == "LLM_CALL"]
    assert len(audit_calls) == 1
    payload = audit_calls[0].args[1]
    assert payload["agent"] == "Fixer"
    assert payload["input_tokens"] == 500
    assert payload["output_tokens"] == 150


@pytest.mark.asyncio
async def test_base_agent_records_error_when_llm_fails():
    from app.agents.base import BaseAgent

    with patch(
        "app.agents.base.ModelRouter.route_and_call_full",
        new=AsyncMock(side_effect=RuntimeError("rate_limit")),
    ), patch(
        "app.agents.base.track_llm_usage_per_agent"
    ) as mock_track, patch(
        "app.agents.base.audit_service.log_event"
    ) as mock_audit, patch(
        "app.agents.base.record_llm_call"
    ):
        agent = BaseAgent(name="Analyzer", role="r")
        with pytest.raises(RuntimeError, match="rate_limit"):
            await agent.ask(user_context="ctx")

    # track_llm_usage_per_agent с error_type
    kwargs = mock_track.call_args.kwargs
    assert kwargs["agent"] == "Analyzer"
    assert kwargs["error_type"] == "RuntimeError"
    assert kwargs["input_tokens"] == 0  # на ошибке usage недоступен

    # audit LLM_CALL_FAILED с error_type
    failed = [c for c in mock_audit.call_args_list if c.args[0] == "LLM_CALL_FAILED"]
    assert len(failed) == 1
    assert failed[0].args[1]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_base_agent_records_empty_response_as_error():
    """Пустой text в response → LLM_CALL_EMPTY audit + error_type='empty_response'."""
    from app.agents.base import BaseAgent

    fake_result = {
        "text": "",  # пустой
        "input_tokens": 200,
        "output_tokens": 0,
        "model": "x",
        "backend": "anthropic",
    }
    with patch(
        "app.agents.base.ModelRouter.route_and_call_full",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "app.agents.base.track_llm_usage_per_agent"
    ) as mock_track, patch(
        "app.agents.base.audit_service.log_event"
    ) as mock_audit, patch(
        "app.agents.base.record_llm_call"
    ):
        agent = BaseAgent(name="Synthesizer", role="r")
        with pytest.raises(ValueError, match="Empty response"):
            await agent.ask(user_context="ctx")

    kwargs = mock_track.call_args.kwargs
    assert kwargs["error_type"] == "empty_response"
    empty_events = [c for c in mock_audit.call_args_list if c.args[0] == "LLM_CALL_EMPTY"]
    assert len(empty_events) == 1


# ── track_llm_usage_per_agent: counter increment ────────────────────────────

def test_track_llm_usage_per_agent_inc_with_zero_tokens_no_op():
    """Не инкрементим counter с zero — иначе нулевые observations засоряют histogram."""
    from app.observability import ai_metrics

    # сохраняем counters для откатывания после теста
    before_input = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="Fixer", model="x", direction="input"
    )._value.get()
    before_output = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="Fixer", model="x", direction="output"
    )._value.get()

    ai_metrics.track_llm_usage_per_agent(
        agent="Fixer", model="x",
        input_tokens=0, output_tokens=0,
        latency_s=1.0,
    )

    after_input = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="Fixer", model="x", direction="input"
    )._value.get()
    after_output = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="Fixer", model="x", direction="output"
    )._value.get()

    assert after_input == before_input  # 0 не инкрементит
    assert after_output == before_output


def test_track_llm_usage_per_agent_inc_with_real_numbers():
    from app.observability import ai_metrics

    before = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="HypAgent", model="claude-sonnet", direction="input"
    )._value.get()
    ai_metrics.track_llm_usage_per_agent(
        agent="HypAgent", model="claude-sonnet",
        input_tokens=1000, output_tokens=250,
        latency_s=2.5,
    )
    after_in = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="HypAgent", model="claude-sonnet", direction="input"
    )._value.get()
    after_out = ai_metrics.LLM_TOKENS_PER_AGENT.labels(
        agent="HypAgent", model="claude-sonnet", direction="output"
    )._value.get()

    assert after_in == before + 1000
    assert after_out >= 250  # >= потому что может уже было >0 от других тестов
