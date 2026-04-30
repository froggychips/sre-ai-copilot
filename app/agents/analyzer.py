from app.agents.base import BaseAgent
from app.models.incident import NewRelicIncident
from app.services.telemetry_utils import trace_agent

class AnalyzerAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Analyzer",
            role="Senior SRE Analyst specializing in log and metric interpretation."
        )

    @trace_agent("Analyzer")
    async def analyze(self, incident: NewRelicIncident) -> str:
        # Передаем данные инцидента как контекст, а инструкцию отдельно
        return await self.ask(
            user_context=incident.model_dump_json(indent=2),
            instruction="Analyze this raw incident data from New Relic. Summarize what is happening technically."
        )
