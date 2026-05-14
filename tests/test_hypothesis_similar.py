"""Tests for HypothesisAgent's similar_past prompt augmentation.

Verifies that the past-incidents context is rendered into the prompt
(bullet shape, score / root_cause / summary keys), that omitting the
parameter keeps the legacy single-arg signature, and that an empty
list is treated identically to None (don't render the section at all).
"""

from unittest.mock import AsyncMock

import pytest

from app.agents.hypothesis import HypothesisAgent


@pytest.mark.asyncio
async def test_generate_without_similar_past_omits_history_section(mocker) -> None:
    agent = HypothesisAgent()
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock
    )
    mock_api.return_value = {
        "text": "1. cause-A\n2. cause-B\n3. cause-C",
        "input_tokens": 50, "output_tokens": 10,
        "model": "test", "backend": "anthropic",
    }

    await agent.generate("Analysis: pod restart loop")

    prompt = mock_api.call_args[0][0]
    assert "Past similar incidents" not in prompt


@pytest.mark.asyncio
async def test_generate_with_empty_similar_past_omits_history_section(mocker) -> None:
    agent = HypothesisAgent()
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock
    )
    mock_api.return_value = {
        "text": "x",
        "input_tokens": 50, "output_tokens": 10,
        "model": "test", "backend": "anthropic",
    }

    await agent.generate("Analysis: x", similar_past=[])

    prompt = mock_api.call_args[0][0]
    assert "Past similar incidents" not in prompt


@pytest.mark.asyncio
async def test_generate_with_similar_past_injects_bullet_list(mocker) -> None:
    agent = HypothesisAgent()
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock
    )
    mock_api.return_value = {
        "text": "x",
        "input_tokens": 50, "output_tokens": 10,
        "model": "test", "backend": "anthropic",
    }

    similar = [
        {
            "incident_id": "ALERT-42",
            "score": 0.8,
            "root_cause": "OOMKilled in init container after image bump",
            "summary": "memory limit too low for new image baseline",
        },
        {
            "incident_id": "ALERT-99",
            "score": 0.6,
            "root_cause": "Liveness probe timeout on cold start",
            "summary": "JVM warm-up exceeded probe deadline",
        },
    ]

    await agent.generate("Analysis: restart loop in squad-1", similar_past=similar)

    prompt = mock_api.call_args[0][0]
    assert "Past similar incidents" in prompt
    assert "score=0.8" in prompt
    assert "OOMKilled in init container" in prompt
    assert "score=0.6" in prompt
    assert "Liveness probe timeout" in prompt
    # Anti-blind-repeat instruction must be there — the whole point of
    # the augmentation is to bias toward, not lock onto, history.
    assert "do not blindly repeat" in prompt.lower()


@pytest.mark.asyncio
async def test_generate_caps_oversized_past_fields(mocker) -> None:
    agent = HypothesisAgent()
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock
    )
    mock_api.return_value = {
        "text": "x",
        "input_tokens": 50, "output_tokens": 10,
        "model": "test", "backend": "anthropic",
    }

    long_text = "x" * 5000
    similar = [{"score": 0.5, "root_cause": long_text, "summary": long_text}]

    await agent.generate("Analysis: y", similar_past=similar)

    prompt = mock_api.call_args[0][0]
    # 200 chars cap on root_cause, 100 on summary — total injected from
    # one entry is bounded.
    assert prompt.count("x") < 5000 + 1000  # guard: most of the long_text is dropped


@pytest.mark.asyncio
async def test_generate_handles_missing_fields_gracefully(mocker) -> None:
    """Real SimilarIncidentEngine sometimes returns sparse dicts."""
    agent = HypothesisAgent()
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock
    )
    mock_api.return_value = {
        "text": "x",
        "input_tokens": 50, "output_tokens": 10,
        "model": "test", "backend": "anthropic",
    }

    # Sparse: only score is present.
    similar = [{"score": 0.5}]

    await agent.generate("Analysis: y", similar_past=similar)

    prompt = mock_api.call_args[0][0]
    assert "score=0.5" in prompt
    # Missing fields rendered as placeholders, not crashes.
    assert "root_cause=?" in prompt
