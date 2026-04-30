from prometheus_client import Counter, Histogram, Gauge

OPENAI_API_CALLS = Counter("openai_api_calls_total", "Total OpenAI API attempts")
OPENAI_API_ERRORS = Counter("openai_api_errors_total", "Total OpenAI API failures")
REQUEST_LATENCY = Histogram("request_duration_seconds", "HTTP latency", buckets=(0.1, 0.5, 1.0, 5.0, float("inf")))
CELERY_QUEUE_LENGTH = Gauge("celery_queue_length", "Pending Celery tasks")

def inc_openai_calls(): OPENAI_API_CALLS.inc()
def inc_openai_errors(): OPENAI_API_ERRORS.inc()
def observe_request_latency(duration): REQUEST_LATENCY.observe(duration)
def set_queue_length(count): CELERY_QUEUE_LENGTH.set(count)
