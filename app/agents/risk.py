from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

class RiskAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="RiskManager",
            role="Security and Stability Auditor. Assess the risk of the suggested fix."
        )

    @trace_agent("RiskManager")
    async def assess(self, fix_suggestion: str) -> str:
        return await self.ask(
            user_context=fix_suggestion,
            instruction="Assign a risk level: LOW, MEDIUM, or HIGH. Explain why. If it involves deleting or restarting critical services, it must be HIGH."
        )
