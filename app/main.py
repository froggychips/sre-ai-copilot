from fastapi import FastAPI, Request, Depends, HTTPException, Body, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.database import engine
from app.api import webhooks
from app.config import settings
from app.middleware import RequestIDMiddleware
from app.auth import get_current_user, User
from app.telemetry import setup_telemetry
from app.metrics import observe_request_latency
from prometheus_client import start_http_server
import structlog
import time
from typing import Optional
from uuid import UUID
from app import repository
from app.models import MessageRole
from app.celery_worker import celery_app, generate_reply
from celery.result import AsyncResult

app_configs = {"title": "SRE AI Copilot", "version": "2.4.0"}
if settings.ENV == "production":
    app_configs.update({"docs_url": None, "redoc_url": None, "openapi_url": None})

app = FastAPI(**app_configs)

# Setup Tracing & Instrumentation
setup_telemetry(app)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    observe_request_latency(time.time() - start_time)
    return response

@app.on_event("startup")
async def startup_event():
    # Start separate metrics server on port 8001
    start_http_server(port=8001)

app.include_router(webhooks.router, prefix="/webhooks")

@app.post("/copilot", status_code=202)
async def post_copilot(
    response: Response,
    conversation_id: Optional[UUID] = Body(None),
    prompt: str = Body(...),
    user: User = Depends(get_current_user)
):
    if not conversation_id:
        conversation_id = await repository.create_conversation()
    await repository.add_message(conv_id=conversation_id, role=MessageRole.user, content=prompt)
    task = generate_reply.delay(str(conversation_id), prompt)
    response.headers["Location"] = f"/jobs/{task.id}"
    return {"task_id": task.id, "conversation_id": conversation_id}

@app.get("/healthz")
async def healthz(): return {"status": "ok"}
@app.get("/readyz")
async def readyz():
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except: raise HTTPException(status_code=503)

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
