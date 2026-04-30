from fastapi import FastAPI, Request, Depends, HTTPException, Body, Response
from sqlalchemy import text
from app.database import engine
from app.api import webhooks
from app.config import settings
from app.middleware import RequestIDMiddleware
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import make_asgi_app, Counter
import structlog
from typing import Optional
from uuid import UUID
from app import repository
from app.models import MessageRole
from app.celery_worker import celery_app, generate_reply
from celery.result import AsyncResult

# Observability configuration
structlog.configure(processors=[structlog.processors.JSONRenderer()])
logger = structlog.get_logger()

trace.set_tracer_provider(TracerProvider())
otlp_exporter = OTLPSpanExporter(endpoint=settings.OTLP_EXPORTER_ENDPOINT, insecure=True)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(otlp_exporter))

app = FastAPI(title="SRE AI Copilot", version="2.0.0")

# Middleware
app.add_middleware(RequestIDMiddleware)

# Metrics
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP Requests", ["method", "endpoint"])

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    return await call_next(request)

app.include_router(webhooks.router, prefix="/webhooks")

@app.post("/copilot", status_code=202)
async def post_copilot(
    response: Response,
    conversation_id: Optional[UUID] = Body(None),
    prompt: str = Body(...)
):
    # 1. Create conversation if ID is not provided
    if not conversation_id:
        conversation_id = await repository.create_conversation()
    
    # 2. Store the user's prompt in the database
    await repository.add_message(
        conv_id=conversation_id,
        role=MessageRole.user,
        content=prompt
    )
    
    # 3. Enqueue the processing task to Celery
    task = generate_reply.delay(str(conversation_id), prompt)
    
    # 4. Set Location header
    response.headers["Location"] = f"/jobs/{task.id}"
    
    return {
        "task_id": task.id,
        "conversation_id": conversation_id
    }

@app.get("/jobs/{task_id}")
async def get_job_status(task_id: str):
    """
    Checks the status of a Celery task and returns its current state and result.
    """
    result = AsyncResult(task_id, app=celery_app)
    
    response_data = {
        "task_id": task_id,
        "status": result.status,
    }

    if result.ready():
        if result.successful():
            response_data["result"] = result.result
        else:
            response_data["error"] = str(result.result)

    return response_data

@app.get("/healthz")
async def healthz():
    """Simple liveness probe."""
    return {"status": "ok"}

@app.get("/readyz")
async def readyz():
    """Readiness probe checking database connectivity."""
    try:
        # Use simple sync connection check for readiness
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        logger.error("readiness_check_failed", error=str(e))
        raise HTTPException(status_code=503, detail="Database connectivity failed")

FastAPIInstrumentor.instrument_app(app)
