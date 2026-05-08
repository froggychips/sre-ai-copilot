from app.agents.base import BaseAgent
from app.models.incident import Incident
from app.services.telemetry_utils import trace_agent


class AnalyzerAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Analyzer",
            role="Senior SRE Analyst specializing in log and metric interpretation.",
        )

    @trace_agent("Analyzer")
    async def analyze(self, incident: Incident) -> str:
        return await self.ask(
            user_context=incident.model_dump_json(indent=2),
            instruction="Analyze this incident from AlertManager. Summarize what is happening technically.",
        )
