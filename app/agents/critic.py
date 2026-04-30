from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

class CriticAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Critic",
            role="Critical SRE Auditor. Filter out weak hypotheses and refine the strongest one."
        )

    @trace_agent("Critic")
    async def audit(self, analysis: str, hypotheses: str) -> str:
        context = f"Analysis: {analysis}\nHypotheses: {hypotheses}"
        return await self.ask(
            user_context=context,
            instruction="Review the hypotheses. Remove ones that don't fit the analysis. Finalize the most likely cause."
        )
