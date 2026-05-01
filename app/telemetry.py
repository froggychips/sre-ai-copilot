import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import settings

logger = structlog.get_logger()

try:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
except Exception:  # optional dependency in local/dev runs
    CeleryInstrumentor = None


def setup_telemetry(app=None):
    resource = Resource.create({"service.name": "copilot-api", "deployment.environment": settings.ENV})

    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=settings.OTLP_EXPORTER_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)

    if app:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("telemetry_instrumented_fastapi")

    if CeleryInstrumentor is not None:
        CeleryInstrumentor().instrument()
        logger.info("telemetry_instrumented_celery")
    else:
        logger.warning("telemetry_celery_instrumentation_skipped")

    return provider
