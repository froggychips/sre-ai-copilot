import time

import structlog

from app.config import settings
from app.core.tracing import record_llm_call
from app.llm.router import ModelRouter
from app.observability.ai_metrics import track_llm_usage_per_agent
from app.services.audit_logger import audit_service
from app.services.prompt_guard import prompt_guard
from app.services.telemetry_utils import record_llm_metrics, tracer

logger = structlog.get_logger()


class BaseAgent:
    def __init__(self, name: str, role: str, task_type: str = "analysis"):
        self.name = name
        self.role = role
        self.task_type = task_type

    async def ask(self, user_context: str, instruction: str = "") -> str:
        with tracer.start_as_current_span(f"LLM_Call: {self.name}") as span:
            span.set_attribute("agent.role", self.role)
            span.set_attribute("agent.name", self.name)

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
            start = time.monotonic()
            recorded = False  # flag: empty-response уже записал свой error, exception-branch пропускает
            try:
                # route_and_call_full возвращает usage с реальными числами
                # из Anthropic response (claude_cli возвращает 0/0).
                result = await ModelRouter.route_and_call_full(self.task_type, full_prompt)
                duration_s = time.monotonic() - start
                duration_ms = int(duration_s * 1000)
                response_text = result.get("text", "") if isinstance(result, dict) else result
                input_tokens = result.get("input_tokens", 0) if isinstance(result, dict) else 0
                output_tokens = result.get("output_tokens", 0) if isinstance(result, dict) else 0
                model_name = (result.get("model") if isinstance(result, dict) else settings.MODEL_NAME) or settings.MODEL_NAME

                if not response_text:
                    record_llm_call(
                        backend=model_name,
                        duration_ms=duration_ms,
                        error="empty_response",
                    )
                    track_llm_usage_per_agent(
                        agent=self.name, model=model_name,
                        input_tokens=input_tokens, output_tokens=0,
                        latency_s=duration_s, error_type="empty_response",
                    )
                    audit_service.log_event("LLM_CALL_EMPTY", {
                        "agent": self.name, "model": model_name,
                        "duration_ms": duration_ms, "input_tokens": input_tokens,
                    })
                    recorded = True
                    raise ValueError("Empty response from model")

                record_llm_call(backend=model_name, duration_ms=duration_ms)
                track_llm_usage_per_agent(
                    agent=self.name, model=model_name,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    latency_s=duration_s,
                )
                audit_service.log_event("LLM_CALL", {
                    "agent": self.name,
                    "model": model_name,
                    "duration_ms": duration_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                })
            except Exception as exc:
                if recorded:
                    raise  # empty-response branch уже всё записал
                duration_s = time.monotonic() - start
                duration_ms = int(duration_s * 1000)
                record_llm_call(
                    backend=settings.MODEL_NAME,
                    duration_ms=duration_ms,
                    error=type(exc).__name__,
                )
                track_llm_usage_per_agent(
                    agent=self.name, model=settings.MODEL_NAME,
                    input_tokens=0, output_tokens=0,
                    latency_s=duration_s, error_type=type(exc).__name__,
                )
                audit_service.log_event("LLM_CALL_FAILED", {
                    "agent": self.name,
                    "model": settings.MODEL_NAME,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                })
                raise

            # OTEL span — реальные числа вместо char-approximation.
            record_llm_metrics(
                span,
                model=model_name,
                usage={
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            )
            span.set_attribute("llm.input_tokens", input_tokens)
            span.set_attribute("llm.output_tokens", output_tokens)
            span.set_attribute("llm.duration_ms", duration_ms)

            return response_text
