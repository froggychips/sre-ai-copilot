import time
from collections import defaultdict
from typing import Optional
from uuid import UUID

from celery.result import AsyncResult
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import start_http_server
from sqlalchemy import text

from app import repository
from app.api import approvals, replay, webhooks
from app.evaluation import feedback
from app.auth import User, get_current_user
from app.celery_worker import celery_app, generate_reply
from app.config import settings
from app.database import engine
from app.metrics import observe_request_latency
from app.middleware import RequestIDMiddleware
from app.models import MessageRole
from app.telemetry import setup_telemetry

app_configs = {"title": "SRE AI Copilot", "version": "2.4.0"}
if settings.ENV == "production":
    app_configs.update({"docs_url": None, "redoc_url": None, "openapi_url": None})

app = FastAPI(**app_configs)
setup_telemetry(app)

app.add_middleware(RequestIDMiddleware)
if settings.ENV == "production":
    allowed_origins = [
        "https://grafana.example.com",
        "https://app.example.com",
    ]  # Replace with actual domains
else:
    allowed_origins = ["http://localhost:3000", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Simple in-memory rate limiter
rate_limit_store = defaultdict(list)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/webhooks/alertmanager":
        client_ip = request.client.host
        now = time.time()
        # Keep only requests in last 60 seconds
        rate_limit_store[client_ip] = [
            t for t in rate_limit_store[client_ip] if now - t < 60
        ]
        if len(rate_limit_store[client_ip]) >= 10:  # 10 requests per minute
            return Response(status_code=429, content="Rate limit exceeded")
        rate_limit_store[client_ip].append(now)
    return await call_next(request)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    observe_request_latency(time.time() - start_time)
    return response
    start_time = time.time()
    response = await call_next(request)
    observe_request_latency(time.time() - start_time)
    return response


@app.on_event("startup")
async def startup_event():
    start_http_server(port=8001)


@app.on_event("shutdown")
async def shutdown_event():
    import structlog

    logger = structlog.get_logger()
    logger.info("application_shutdown")

    # Graceful Celery shutdown
    celery_app.control.shutdown()

    # Close DB pool
    await engine.dispose()


app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(approvals.router, prefix="/approvals", tags=["approvals"])
app.include_router(replay.router, prefix="/replay", tags=["replay"])
app.include_router(feedback.router, prefix="/evaluation", tags=["evaluation"])


@app.post("/copilot", status_code=202)
async def post_copilot(
    response: Response,
    conversation_id: Optional[UUID] = Body(None),
    prompt: str = Body(...),
    user: User = Depends(get_current_user),
):
    if not conversation_id:
        conversation_id = await repository.create_conversation()
    await repository.add_message(
        conv_id=conversation_id, role=MessageRole.user, content=prompt
    )
    task = generate_reply.delay(str(conversation_id), prompt)
    response.headers["Location"] = f"/jobs/{task.id}"
    return {"task_id": task.id, "conversation_id": conversation_id}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(
            status_code=503, detail=f"Database connectivity failed: {e}"
        )


@app.get("/jobs/{task_id}")
async def get_job_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    response_data = {"task_id": task_id, "status": result.status}

    if result.ready():
        if result.successful():
            response_data["result"] = result.result
        else:
            response_data["error"] = str(result.result)

    return response_data
