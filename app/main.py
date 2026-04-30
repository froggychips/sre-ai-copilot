from fastapi import FastAPI, Request, Depends, HTTPException
from app.api import webhooks
from app.config import settings
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import make_asgi_app, Counter
import structlog
import uuid

# Observability
structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger()

trace.set_tracer_provider(TracerProvider())
otlp_exporter = OTLPSpanExporter(endpoint=settings.OTLP_EXPORTER_ENDPOINT, insecure=True)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(otlp_exporter))

app = FastAPI(title="SRE AI Copilot", version="2.0.0")

# Metrics
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP Requests", ["method", "endpoint"])

@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(webhooks.router, prefix="/webhooks")

@app.get("/healthz")
async def healthz(): return {"status": "ok"}

@app.get("/readyz")
async def readyz(): return {"status": "ready"}

FastAPIInstrumentor.instrument_app(app)
