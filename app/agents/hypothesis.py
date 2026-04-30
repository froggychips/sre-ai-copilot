from app.agents.base import BaseAgent

class HypothesisAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Hypothesis",
            role="SRE Problem Solver. Generate possible root causes for the given incident analysis."
        )

    async def generate(self, analysis: str) -> str:
        prompt = f"Based on this analysis: {analysis}\nList 3 most likely root causes. Rank them by probability."
        return await self.ask(prompt)
