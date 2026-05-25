from prometheus_client import Counter, Gauge, Histogram

REQUEST_LATENCY = Histogram(
    "request_duration_seconds",
    "HTTP latency",
    buckets=(0.1, 0.5, 1.0, 5.0, float("inf")),
)
CELERY_QUEUE_LENGTH = Gauge("celery_queue_length", "Pending Celery tasks")

# A3: счётчик отфильтрованных alerts на входе webhook handler-а.
# `reason` — категория фильтра:
#   "allowlist"        — alertname matched ALERT_SUPPRESS_NAMES (A3)
#   "inhibited_warn"   — AM inhibit + не critical → skip Discord (A1)
#   "inhibited_info"   — AM inhibit + severity=info (всегда skip)
# Labels отдельно по alertname — кардинальность ограничена (allowlist
# короткий, AM-suppressed events редкие). Метрика scrape-ится /metrics.
ALERTS_SUPPRESSED = Counter(
    "alerts_suppressed_total",
    "Alerts filtered out before enrich/Discord (allowlist or AM-inhibit gate)",
    ["reason", "alertname"],
)


def observe_request_latency(duration):
    REQUEST_LATENCY.observe(duration)


def set_queue_length(count):
    CELERY_QUEUE_LENGTH.set(count)
