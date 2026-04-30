from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

class HypothesisAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Hypothesis",
            role="SRE Problem Solver. Generate possible root causes for the given incident analysis."
        )

    @trace_agent("Hypothesis")
    async def generate(self, analysis: str) -> str:
        return await self.ask(
            user_context=analysis,
            instruction="List 3 most likely root causes based on this analysis. Rank them by probability."
        )
