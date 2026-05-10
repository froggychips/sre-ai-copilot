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
                "Synthesize the 5-stage pipeline output into a single structured report.\n\n"
                "**What happened** — one sentence, blast radius\n"
                "**Root cause** — one sentence with supporting evidence\n"
                "**Fix** — concrete steps, priority-ordered\n"
                "**Risk** — LOW / MEDIUM / HIGH and the key concern\n"
                "**Confidence** — 0–100% and what would increase it\n\n"
                "Be direct. No padding. Engineers will act on this."
            ),
        )
