import time

from app.llm.router import ModelRouter
from app.services.prompt_guard import prompt_guard
from app.services.telemetry_utils import tracer, record_llm_metrics
from app.config import settings
from app.core.tracing import record_llm_call
import structlog

logger = structlog.get_logger()

class BaseAgent:
    def __init__(self, name: str, role: str, task_type: str = "analysis"):
        self.name = name
        self.role = role
        self.task_type = task_type

    async def ask(self, user_context: str, instruction: str = "") -> str:
        with tracer.start_as_current_span(f"LLM_Call: {self.name}") as span:
            span.set_attribute("agent.role", self.role)

            is_attack, reason = prompt_guard.detect_injection(user_context)
            if is_attack:
                raise PermissionError(f"Security Policy Block: {reason}")

            safe_context = prompt_guard.sanitize(user_context)

            full_prompt = f"""
Role: {self.role}
Task: {instruction}
<user_context>
{safe_context}
</user_context>
"""
            # Time the LLM round-trip and publish to the active StageTimer
            # (if any) so post-mortem can read durations/backends directly
            # off the saved incident row, not just from OTel.
            start = time.monotonic()
            try:
                response = await ModelRouter.route_and_call(self.task_type, full_prompt)
                duration_ms = int((time.monotonic() - start) * 1000)
                if not response:
                    record_llm_call(backend=settings.MODEL_NAME, duration_ms=duration_ms, error="empty_response")
                    raise ValueError("Empty response from model")
                record_llm_call(backend=settings.MODEL_NAME, duration_ms=duration_ms)
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                record_llm_call(
                    backend=settings.MODEL_NAME,
                    duration_ms=duration_ms,
                    error=type(exc).__name__,
                )
                raise

            record_llm_metrics(span, model=settings.MODEL_NAME, usage={
                "total_tokens": len(full_prompt) // 4 + len(response) // 4
            })

            return response
