from app.agents.base import BaseAgent
from app.context.k8s_facts import K8sFacts
from app.services.telemetry_utils import trace_agent


class CriticAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Critic",
            role="Critical SRE Auditor. Filter out weak hypotheses and refine the strongest one.",
        )

    @trace_agent("Critic")
    async def audit(
        self, analysis: str, hypotheses: str, namespace: str | None = None
    ) -> str:
        facts = ""
        if namespace:
            facts = await K8sFacts.collect(namespace)

        context = f"Analysis: {analysis}\nHypotheses: {hypotheses}"
        if facts:
            context += f"\n\nVerified facts from cluster:\n{facts}"

        return await self.ask(
            user_context=context,
            instruction=(
                "Review the hypotheses against the verified cluster facts. "
                "Remove ones contradicted by facts. "
                "State the blast radius explicitly (is this one pod, one namespace, or wider?). "
                "Finalize the most likely cause with supporting evidence from the facts."
            ),
        )
