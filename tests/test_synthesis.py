"""Unit-тесты для SynthesisAgent (Stage 6 пайплайна).

Что важно проверить:
  * Контракт промпта: все 5 секций входов попадают в user_context.
  * incident_id фигурирует в prompt-е.
  * Инструкция содержит обязательный пункт "Fix addresses root cause?"
    (это был баг до коммита f1fcd22 — пункт пропадал).
  * Возврат — то, что вернул LLM, без post-processing-а.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.synthesis import SynthesisAgent


@pytest.fixture
def captured_ask():
    """Перехватывает BaseAgent.ask и возвращает (user_context, instruction)."""
    async def _ask(self, user_context, instruction=""):
        captured["user_context"] = user_context
        captured["instruction"] = instruction
        return "SYNTHESIS REPORT GOES HERE"

    captured: dict = {}
    with patch("app.agents.base.BaseAgent.ask", new=_ask):
        yield captured


@pytest.mark.asyncio
async def test_synthesis_returns_llm_output(captured_ask):
    agent = SynthesisAgent()
    out = await agent.synthesize(
        incident_id="inc-42",
        analysis="ANALYSIS",
        hypotheses="H1, H2",
        final_cause="OOM in payment-svc",
        fix_suggestion="bump memory limit",
        risk_report="LOW",
    )
    assert out == "SYNTHESIS REPORT GOES HERE"


@pytest.mark.asyncio
async def test_synthesis_packs_all_five_sections(captured_ask):
    agent = SynthesisAgent()
    await agent.synthesize(
        incident_id="inc-42",
        analysis="A-CONTENT",
        hypotheses="H-CONTENT",
        final_cause="C-CONTENT",
        fix_suggestion="F-CONTENT",
        risk_report="R-CONTENT",
    )
    ctx = captured_ask["user_context"]
    assert "inc-42" in ctx
    assert "=== ANALYZER ===" in ctx
    assert "A-CONTENT" in ctx
    assert "=== HYPOTHESES ===" in ctx
    assert "H-CONTENT" in ctx
    assert "=== CRITIC (Root Cause) ===" in ctx
    assert "C-CONTENT" in ctx
    assert "=== FIX ===" in ctx
    assert "F-CONTENT" in ctx
    assert "=== RISK ===" in ctx
    assert "R-CONTENT" in ctx


@pytest.mark.asyncio
async def test_synthesis_instruction_includes_root_cause_check(captured_ask):
    """Регрессия для f1fcd22 — Fix-addresses-root-cause пункт обязателен."""
    agent = SynthesisAgent()
    await agent.synthesize(
        incident_id="x",
        analysis="",
        hypotheses="",
        final_cause="",
        fix_suggestion="",
        risk_report="",
    )
    instruction = captured_ask["instruction"]
    assert "Fix addresses root cause?" in instruction
    assert "YES or NO" in instruction


@pytest.mark.asyncio
async def test_synthesis_instruction_demands_structured_report(captured_ask):
    agent = SynthesisAgent()
    await agent.synthesize(
        incident_id="x", analysis="", hypotheses="",
        final_cause="", fix_suggestion="", risk_report="",
    )
    instruction = captured_ask["instruction"]
    # Каждая обязательная секция итогового отчёта.
    for required in [
        "What happened",
        "Root cause",
        "Fix",
        "Risk",
        "Confidence",
    ]:
        assert required in instruction, f"missing section: {required}"


@pytest.mark.asyncio
async def test_synthesis_propagates_llm_errors():
    """Если LLM падает — SynthesisAgent пробрасывает исключение, не глушит."""
    agent = SynthesisAgent()
    with patch(
        "app.agents.base.BaseAgent.ask",
        new=AsyncMock(side_effect=ValueError("backend down")),
    ):
        with pytest.raises(ValueError, match="backend down"):
            await agent.synthesize(
                incident_id="x", analysis="", hypotheses="",
                final_cause="", fix_suggestion="", risk_report="",
            )
