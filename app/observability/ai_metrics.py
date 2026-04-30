from prometheus_client import Counter, Histogram, Summary

# Model usage & performance
LLM_LATENCY = Histogram("llm_request_duration_seconds", "LLM API latency", ["model"])
TOKEN_USAGE = Counter("llm_tokens_total", "Total tokens used", ["model", "type"])
API_ERRORS = Counter("llm_api_errors_total", "API error count", ["model", "error_type"])

def track_llm_metrics(model: str, latency: float, prompt_tokens: int, completion_tokens: int):
    LLM_LATENCY.labels(model=model).observe(latency)
    TOKEN_USAGE.labels(model=model, type="prompt").inc(prompt_tokens)
    TOKEN_USAGE.labels(model=model, type="completion").inc(completion_tokens)
