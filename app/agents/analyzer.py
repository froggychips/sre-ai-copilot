from app.agents.base import BaseAgent
from app.models.incident import NewRelicIncident

class AnalyzerAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Analyzer",
            role="Senior SRE Analyst specializing in log and metric interpretation."
        )

    async def analyze(self, incident: NewRelicIncident) -> str:
        prompt = f"Analyze this raw incident data from New Relic:\n{incident.model_dump_json(indent=2)}\nSummarize what is happening technically."
        return await self.ask(prompt)
