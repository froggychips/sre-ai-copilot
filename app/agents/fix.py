from app.agents.base import BaseAgent
from app.services.telemetry_utils import trace_agent

_BASE_INSTRUCTION = """
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

# При рецидиве стандартный фикс ("перезапусти") почти наверняка не поможет —
# он уже применялся. Агент должен фокусироваться на расследовании, не на mitigation.
_RECURRENCE_PREFIX = """
CRITICAL CONTEXT: This is a RECURRING incident.
The same service experienced the same root cause recently and was marked as resolved,
but the issue has returned. The previous fix did NOT hold.

Do NOT recommend a simple restart or rollback — those have already been tried.
Instead, recommend an investigative action (get_logs, describe_resource) to gather
evidence for a deeper root-cause fix: memory leak, misconfiguration, dependency bug,
or infrastructure regression.

"""


class FixAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Fixer",
            role="Kubernetes Expert. Generate structured execution intents to fix the incident.",
        )

    @trace_agent("Fixer")
    async def suggest(self, finalized_cause: str, is_recurrence: bool = False) -> str:
        instruction = (
            _RECURRENCE_PREFIX + _BASE_INSTRUCTION if is_recurrence else _BASE_INSTRUCTION
        )
        return await self.ask(user_context=finalized_cause, instruction=instruction)
