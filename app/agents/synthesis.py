from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent


class SynthesisAgent(BaseAgent):
    """
    Stage 6: sees all 5 pipeline outputs simultaneously and produces
    a single coherent, actionable incident report.

    Unlike stages 1-5 (chained, each sees only previous output),
    the synthesizer has the full context — this is the second reasoning level.
    """

    def __init__(self):
        super().__init__(
            name="Synthesizer",
            role=(
                "Senior SRE. You receive the complete output of a 5-stage incident "
                "analysis pipeline and synthesize it into one coherent, actionable report."
            ),
        )

    @trace_agent("Synthesizer")
    async def synthesize(
        self,
        incident_id: str,
        analysis: str,
        hypotheses: str,
        final_cause: str,
        fix_suggestion: str,
        risk_report: str,
    ) -> str:
        context = f"""Incident ID: {incident_id}

=== ANALYZER ===
{analysis}

=== HYPOTHESES ===
{hypotheses}

=== CRITIC (Root Cause) ===
{final_cause}

=== FIX ===
{fix_suggestion}

=== RISK ===
{risk_report}"""

        return await self.ask(
            user_context=context,
            instruction=(
                "Синтезируй результаты 5 стадий в краткий структурированный отчёт на **русском языке**.\n\n"
                "**Что случилось** — одно предложение, blast radius\n"
                "**Причина** — одно предложение с ключевыми доказательствами\n"
                "**Исправление** — 2–4 конкретных шага, по приоритету\n"
                "**Фикс устраняет причину?** — ДА / НЕТ. Если НЕТ: одно предложение с альтернативой.\n"
                "**Риск** — НИЗКИЙ / СРЕДНИЙ / ВЫСОКИЙ и ключевая угроза\n"
                "**Уверенность** — 0–100% и что бы её повысило\n\n"
                "Технические термины, названия сервисов, команды — оставлять на английском.\n"
                "Цель — максимум информации минимумом слов. Весь отчёт должен умещаться в ~800 символов."
            ),
        )
