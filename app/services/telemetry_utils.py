import functools
from contextlib import contextmanager
from typing import Any, Dict, Optional

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("sre_ai_agents")
_pipeline_tracer = trace.get_tracer("sre.copilot.pipeline")
_dsl_tracer = trace.get_tracer("sre.copilot.dsl")
_approval_tracer = trace.get_tracer("sre.copilot.approval")
logger = structlog.get_logger()


def trace_agent(agent_name: str):
    """Decorator: creates a span around an agent method."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(f"sre.copilot.agent.{agent_name.lower()}") as span:
                span.set_attribute("sre.agent.name", agent_name)
                try:
                    result = await func(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    logger.error(
                        "agent_execution_failed", agent=agent_name, error=str(e)
                    )
                    raise e

        return wrapper

    return decorator


@contextmanager
def incident_span(incident_id: str, service: str = "", namespace: str = "", source: str = "alertmanager"):
    """Root span for the entire incident processing lifecycle.

    Usage:
        with incident_span(incident_id, service, namespace) as span:
            span.set_attribute("sre.incident.resolution_quality", "resolved")
    """
    with _pipeline_tracer.start_as_current_span(
        "sre.copilot.incident.process",
        kind=trace.SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("sre.incident.id", incident_id)
        span.set_attribute("sre.incident.service", service)
        span.set_attribute("sre.incident.namespace", namespace)
        span.set_attribute("sre.user.source", source)
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


@contextmanager
def execution_intent_span(action: str, resource_type: str, resource_name: str,
                           namespace: str, risk: str, intent_json: str = ""):
    """Span for ExecutionIntent creation and DSL translation — the key audit record."""
    with _dsl_tracer.start_as_current_span("sre.copilot.execution.intent") as span:
        span.set_attribute("sre.execution.intent.action", action)
        span.set_attribute("sre.execution.intent.resource_type", resource_type)
        span.set_attribute("sre.execution.intent.resource_name", resource_name)
        span.set_attribute("sre.execution.intent.namespace", namespace)
        span.set_attribute("sre.risk.level", risk)
        if intent_json:
            span.set_attribute("sre.execution.intent.dsl", intent_json[:2000])
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


@contextmanager
def approval_span(approval_id: str, action: str, user_id: str = "", risk: str = ""):
    """Span for approval lifecycle events (request / approve / reject)."""
    with _approval_tracer.start_as_current_span(f"sre.copilot.approval.{action}") as span:
        span.set_attribute("sre.approval.id", approval_id)
        span.set_attribute("sre.approval.action", action)
        if user_id:
            span.set_attribute("sre.approval.user_id", user_id)
        if risk:
            span.set_attribute("sre.risk.level", risk)
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def record_llm_metrics(span, model: str, usage: Optional[Dict[str, Any]] = None):
    """Records LLM usage attributes onto a span."""
    span.set_attribute("llm.model_name", model)
    if usage:
        span.set_attribute("llm.prompt_tokens", usage.get("prompt_tokens", 0))
        span.set_attribute("llm.completion_tokens", usage.get("completion_tokens", 0))
        span.set_attribute("llm.total_tokens", usage.get("total_tokens", 0))
