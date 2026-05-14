import socket
from urllib.parse import urlparse

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


def _parse_otlp_endpoint(endpoint: str) -> tuple[str, int] | None:
    """Достать (host, port) из OTLP_EXPORTER_ENDPOINT.

    Поддерживает оба формата: `http://host:port` и `host:port`.
    Возвращает None если endpoint пустой / нечитаемый.
    """
    if not endpoint:
        return None
    try:
        if "://" in endpoint:
            parsed = urlparse(endpoint)
            host = parsed.hostname
            port = parsed.port or 4317
        else:
            host, _, port_s = endpoint.partition(":")
            port = int(port_s) if port_s else 4317
        if not host:
            return None
        return host, port
    except (ValueError, TypeError):
        return None


def _otlp_reachable(endpoint: str, timeout: float = 2.0) -> bool:
    """Быстрая TCP-проверка достижимости OTLP-collector-а.

    Используется один раз при startup. Если endpoint недоступен — экспортер
    не подключается, чтобы не спамить логи `Transient error... UNAVAILABLE`
    каждые секунды. Если jaeger/tempo поднимется потом — нужен рестарт pod-а.
    """
    parsed = _parse_otlp_endpoint(endpoint)
    if parsed is None:
        return False
    try:
        with socket.create_connection(parsed, timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def setup_telemetry(app=None, service_name: str = "copilot-api"):
    if not _OTEL_AVAILABLE:
        logger.warning("telemetry.skipped", reason="otel_sdk_unavailable")
        return None

    from app.config import settings

    resource = Resource.create(
        {"service.name": service_name, "deployment.environment": settings.ENV}
    )
    provider = TracerProvider(resource=resource)

    # Probe OTLP endpoint reachability — fail-fast при unreachable collector,
    # чтобы не накапливать Transient-error retry-спам в логах.
    # Spans всё равно создаются (StageTimer и др. зависят от provider-а),
    # просто не экспортируются.
    endpoint = settings.OTLP_EXPORTER_ENDPOINT
    if _otlp_reachable(endpoint):
        otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("telemetry.exporter_attached", endpoint=endpoint)
    else:
        logger.warning(
            "telemetry.otlp_unreachable_skip_exporter",
            endpoint=endpoint,
            note="spans tracked but not exported; restart pod when collector is up",
        )

    trace.set_tracer_provider(provider)
    logger.info("telemetry.provider_set", service=service_name)

    if app and _FASTAPI_INSTR:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("telemetry.fastapi_instrumented")

    if _CELERY_INSTR:
        CeleryInstrumentor().instrument()
        logger.info("telemetry.celery_instrumented")

    return provider
