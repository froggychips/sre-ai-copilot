import structlog

logger = structlog.get_logger()

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    _OTEL_AVAILABLE = True
except Exception as _otel_err:
    _OTEL_AVAILABLE = False
    logger.warning("telemetry.otel_unavailable", error=str(_otel_err))

try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    _FASTAPI_INSTR = True
except Exception:
    _FASTAPI_INSTR = False

try:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    _CELERY_INSTR = True
except Exception:
    _CELERY_INSTR = False


def setup_telemetry(app=None, service_name: str = "copilot-api"):
    if not _OTEL_AVAILABLE:
        logger.warning("telemetry.skipped", reason="otel_sdk_unavailable")
        return None

    from app.config import settings

    resource = Resource.create(
        {"service.name": service_name, "deployment.environment": settings.ENV}
    )
    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(
        endpoint=settings.OTLP_EXPORTER_ENDPOINT, insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)
    logger.info("telemetry.provider_set", service=service_name)

    if app and _FASTAPI_INSTR:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("telemetry.fastapi_instrumented")

    if _CELERY_INSTR:
        CeleryInstrumentor().instrument()
        logger.info("telemetry.celery_instrumented")

    return provider
