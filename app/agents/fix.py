from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

class FixAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Fixer",
            role="Kubernetes Expert. Generate structured execution intents to fix the incident."
        )

    @trace_agent("Fixer")
    async def suggest(self, finalized_cause: str) -> str:
        instruction = """
        Suggest a Kubernetes fix using a Structured Execution Intent.
        
        Output ONLY a valid JSON object matching this schema:
        {
            "action": "restart_deployment | scale_deployment | get_logs | describe_resource",
            "resource_type": "deployment | pod",
            "resource_name": "string",
            "namespace": "string",
            "params": { "replicas": 1 },
            "risk": "low | medium | high"
        }
        Do not include markdown or extra text.
        """
        return await self.ask(
            user_context=finalized_cause,
            instruction=instruction
        )
