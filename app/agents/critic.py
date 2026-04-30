from app.agents.base import BaseAgent

class CriticAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Critic",
            role="Critical SRE Auditor. Filter out weak hypotheses and refine the strongest one."
        )

    async def audit(self, analysis: str, hypotheses: str) -> str:
        prompt = f"Analysis: {analysis}\nHypotheses: {hypotheses}\nReview the hypotheses. Remove ones that don't fit the analysis. Finalize the most likely cause."
        return await self.ask(prompt)
