import functools
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

# Инициализируем трейсер для агентов
tracer = trace.get_tracer("sre_ai_agents")
logger = structlog.get_logger()

def trace_agent(agent_name: str):
    """
    Декоратор для автоматического создания спана вокруг методов анализа агента.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(f"Agent: {agent_name}") as span:
                span.set_attribute("agent.name", agent_name)
                try:
                    result = await func(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    logger.error("agent_execution_failed", agent=agent_name, error=str(e))
                    raise e
        return wrapper
    return decorator

def record_llm_metrics(span, model: str, usage: dict = None):
    """
    Записывает детальные атрибуты использования LLM в спан.
    """
    span.set_attribute("llm.model_name", model)
    if usage:
        span.set_attribute("llm.prompt_tokens", usage.get("prompt_tokens", 0))
        span.set_attribute("llm.completion_tokens", usage.get("completion_tokens", 0))
        span.set_attribute("llm.total_tokens", usage.get("total_tokens", 0))
