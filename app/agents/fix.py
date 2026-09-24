from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence, Tuple

from app.agents.base import BaseAgent
from app.core.execution_dsl import ExecutionIntent
from app.services.telemetry_utils import trace_agent

if TYPE_CHECKING:
    from app.remediation.playbook import Playbook

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


def _build_jira_prefix(jira_context: Dict[str, Any]) -> str:
    """Форматирует Jira-контекст как преамбулу для FixAgent."""
    lines = ["=== KNOWN JIRA ISSUES ==="]
    for issue in jira_context.get("open", []):
        lines.append(
            f"[OPEN]     {issue['key']} [{issue['priority']}] {issue['summary']} — {issue['url']}"
        )
    for issue in jira_context.get("resolved", []):
        lines.append(
            f"[RESOLVED] {issue['key']} {issue['summary']} — {issue['url']}"
        )
    if jira_context.get("has_open"):
        lines.append(
            "\nIMPORTANT: There are OPEN Jira issues for this service. "
            "The fix should reference the existing issue and focus on mitigation "
            "or escalation, NOT just a restart."
        )
    return "\n".join(lines)


def _build_playbook_prefix(playbooks: Sequence["Playbook"]) -> str:
    """Кандидаты-playbook-и, отобранные детерминированно (matcher).

    Модель выбирает среди них, а не сочиняет действие: executor_gate с
    REMEDIATION_PLAYBOOK_BINDING_ENABLED заблокирует мутирующий intent без
    `playbook` или с действием вне его плана. Описание playbook-а — наш YAML,
    не данные инцидента, так что в промпт оно попадает без экранирования.
    """
    lines = ["=== ALLOWED REMEDIATION PLAYBOOKS ==="]
    for pb in playbooks:
        actions = ", ".join(step.action for step in pb.plan.steps or ())
        desc = " ".join((pb.description or "").split())
        lines.append(f"- {pb.name}: actions [{actions}] — {desc}")
    lines.append(
        "\nIf you propose a state-changing action (restart_deployment, "
        "scale_deployment), it MUST be one of the actions of a playbook above, "
        'and the JSON MUST include "playbook": "<playbook name>". '
        "If none of the playbooks fits, propose a read-only action "
        "(get_logs, describe_resource) without a playbook."
    )
    return "\n".join(lines)


class FixAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Fixer",
            role="Kubernetes Expert. Generate structured execution intents to fix the incident.",
        )

    @trace_agent("Fixer")
    async def suggest(
        self,
        finalized_cause: str,
        is_recurrence: bool = False,
        jira_context: Optional[Dict[str, Any]] = None,
        playbooks: Optional[Sequence["Playbook"]] = None,
    ) -> Tuple[str, Optional[ExecutionIntent]]:
        """Вернуть пару (raw LLM-ответ, распарсенный ExecutionIntent).

        LLM инструктируется выдавать JSON по схеме ExecutionIntent. Если парсинг
        или валидация не прошли — intent=None (advisory-fallback: prose всё равно
        показывается в Discord-embed, executor-стадия просто пропускается).

        `playbooks` — кандидаты от `matcher.match_playbooks`; передаются
        только при REMEDIATION_PLAYBOOK_BINDING_ENABLED. None/пусто — промпт
        прежний.
        """
        instruction = (
            _RECURRENCE_PREFIX + _BASE_INSTRUCTION if is_recurrence else _BASE_INSTRUCTION
        )
        context = finalized_cause
        if jira_context:
            context = _build_jira_prefix(jira_context) + "\n\n" + finalized_cause
        if playbooks:
            context = _build_playbook_prefix(playbooks) + "\n\n" + context
        raw = await self.ask(user_context=context, instruction=instruction)
        intent = ExecutionIntent.from_llm_response(raw)
        return raw, intent
