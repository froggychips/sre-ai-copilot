from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

class FixAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Fixer",
            role="Kubernetes Expert. Generate exact kubectl commands or YAML to fix the incident."
        )

    @trace_agent("Fixer")
    async def suggest(self, finalized_cause: str) -> str:
        return await self.ask(
            user_context=finalized_cause,
            instruction="Suggest a Kubernetes fix. Provide exact kubectl commands. Be conservative."
        )
