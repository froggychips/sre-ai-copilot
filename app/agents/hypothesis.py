from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

# Cap on how much of any single past-incident field we paste into the prompt.
# Avoids one runaway summary blowing up the context window.
_PAST_ROOT_CAUSE_CHARS = 200
_PAST_SUMMARY_CHARS = 100


class HypothesisAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Hypothesis",
            role="SRE Problem Solver. Generate possible root causes for the given incident analysis."
        )

    @trace_agent("Hypothesis")
    async def generate(self, analysis: str, similar_past: list[dict] | None = None) -> str:
        """Generate ranked root cause hypotheses.

        Optionally augmented with `similar_past` — a short list of previously
        ACCEPTED-resolved incidents pulled by SimilarIncidentEngine (matched
        by service / cause / namespace). The agent is told to consider them
        as patterns, not blindly repeat — explicit instruction to avoid the
        "always blame the last thing that broke" failure mode.

        Pattern adapted from froggy-sre HypothesisAgent.run(similarPast:):
        https://github.com/froggychips/froggy-sre/blob/main/Sources/FroggySRECore/Agents.swift
        """
        instruction = (
            "List 3 most likely root causes based on this analysis. "
            "Rank them by probability."
        )
        user_context = analysis

        if similar_past:
            bullets = "\n".join(
                f"- score={p.get('score', '?')} | "
                f"root_cause={(p.get('root_cause') or '?')[:_PAST_ROOT_CAUSE_CHARS]} | "
                f"summary={(p.get('summary') or '')[:_PAST_SUMMARY_CHARS]}"
                for p in similar_past
            )
            user_context = (
                f"{analysis}\n\n"
                f"Past similar incidents that were marked as ACCEPTED resolutions "
                f"(most relevant first):\n{bullets}\n\n"
                f"Consider these patterns when ranking causes, but verify they fit "
                f"the current evidence — do not blindly repeat past hypotheses."
            )

        return await self.ask(user_context=user_context, instruction=instruction)
