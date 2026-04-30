from app.agents.base import BaseAgent

class FixAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Fixer",
            role="Kubernetes Expert. Generate exact kubectl commands or YAML to fix the incident."
        )

    async def suggest(self, finalized_cause: str) -> str:
        prompt = f"Root Cause: {finalized_cause}\nSuggest a Kubernetes fix. Provide exact kubectl commands. Be conservative."
        return await self.ask(prompt)
