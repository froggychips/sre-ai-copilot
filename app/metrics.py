from prometheus_client import Histogram, Gauge

REQUEST_LATENCY = Histogram("request_duration_seconds", "HTTP latency", buckets=(0.1, 0.5, 1.0, 5.0, float("inf")))
CELERY_QUEUE_LENGTH = Gauge("celery_queue_length", "Pending Celery tasks")

def observe_request_latency(duration): REQUEST_LATENCY.observe(duration)
def set_queue_length(count): CELERY_QUEUE_LENGTH.set(count)
