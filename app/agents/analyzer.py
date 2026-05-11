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
            instruction=(
                "Analyze this incident from AlertManager. Summarize what is happening technically. "
                "If `teamcity_context.recent_builds` is present, explicitly correlate the alert with "
                "any deploys finished shortly before the alert started: note matching build IDs, "
                "branch, status, and authors of recent changes. If no TC builds preceded the alert, say so."
            ),
        )
