from app.agents.base import BaseAgent

class RiskAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="RiskManager",
            role="Security and Stability Auditor. Assess the risk of the suggested fix."
        )

    async def assess(self, fix_suggestion: str) -> str:
        prompt = f"Suggested Fix: {fix_suggestion}\nAssign a risk level: LOW, MEDIUM, or HIGH. Explain why. If it involves deleting or restarting critical services, it must be HIGH."
        return await self.ask(prompt)
