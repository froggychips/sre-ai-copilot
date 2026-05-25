import time
from contextlib import asynccontextmanager
from typing import Optional
from uuid import UUID

import structlog
from celery.result import AsyncResult
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import start_http_server
from sqlalchemy import text

from app import repository
from app.api import approvals, discord_interactions, rate_limit, replay, webhooks
from app.evaluation import feedback
from app.auth import User, get_current_user
from app.celery_worker import celery_app, generate_reply
from app.config import settings
from app.database import SessionLocal, engine
from app.knowledge_graph.contract import (
    QUALITY_THRESHOLDS,
    STARTUP_CONTRACT_CHECK,
)
from app.metrics import observe_request_latency
from app.middleware import RequestIDMiddleware
from app.models import MessageRole
from app.telemetry import setup_telemetry

log = structlog.get_logger()


def _run_startup_contract_check() -> None:
    """Wrapper для STARTUP_CONTRACT_CHECK с graceful error handling.

    Логика:
      * Открываем SessionLocal(), вызываем contract check, закрываем.
      * Если settings.STARTUP_CONTRACT_CHECK_ENABLED=False — skip целиком.
      * Если БД недоступна / `kg_services` ещё не создана (например
        in-memory sqlite до Base.metadata.create_all) — graceful skip
        с warning, не падаем.
      * Если report содержит unknown_edge_kinds или orphan_pct > порог —
        логируем warning. Healthy — info.
    Никогда не throws — это диагностический шаг при boot.
    """
    if not getattr(settings, "STARTUP_CONTRACT_CHECK_ENABLED", True):
        log.info("kg_contract.startup_check_disabled")
        return

    db = None
    try:
        db = SessionLocal()
        report = STARTUP_CONTRACT_CHECK(db)
    except Exception as exc:  # pragma: no cover - boot-time safety net
        # БД может быть недоступна на самом раннем шаге boot (в тестах /
        # перед миграциями). Не блокируем startup.
        log.warning("kg_contract.startup_check_skipped", error=str(exc))
        return
    finally:
        if db is not None:
            db.close()

    unknown_edge_kinds = report.get("unknown_edge_kinds") or []
    orphan_pct = report.get("orphan_pct")
    owner_pct = report.get("owner_pct")
    orphan_threshold = QUALITY_THRESHOLDS["orphan_rate_max_pct"]

    warning_level = bool(unknown_edge_kinds)
    if isinstance(orphan_pct, (int, float)) and orphan_pct > orphan_threshold:
        warning_level = True

    log_fn = log.warning if warning_level else log.info
    log_fn(
        "kg_contract.startup_check",
        schema_version=report.get("schema_version"),
        unknown_edge_kinds=unknown_edge_kinds,
        planned_in_db=report.get("planned_in_db") or [],
        orphan_pct=orphan_pct,
        owner_pct=owner_pct,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: поднимаем prometheus-сервер.
    start_http_server(port=8001)
    log.info("application_startup", prometheus_port=8001)
    # KG contract drift guard — read-only diagnostic, не блокирует boot.
    _run_startup_contract_check()
    yield
    # Shutdown: закрытие локальных ресурсов только.
    # engine — синхронный sqlalchemy.Engine (create_engine), dispose() не awaitable.
    #
    # ВАЖНО: НЕ вызываем celery_app.control.shutdown() — это broker-wide
    # broadcast через Redis, который шатдаунит ВСЕ worker-ы в кластере при
    # любом rolling restart api-pod. Worker-ы реагируют на SIGTERM от k8s сами.
    # Обнаружено на проде после rolling restart api с v0.6 → v0.7.0.
    log.info("application_shutdown")
    await rate_limit.close()
    engine.dispose()


# В production отключаем интерактивные docs/openapi: меньше surface для probing.
if settings.ENV == "production":
    app = FastAPI(
        title="SRE AI Copilot",
        version="2.4.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
else:
    app = FastAPI(title="SRE AI Copilot", version="2.4.0", lifespan=lifespan)
setup_telemetry(app)

app.add_middleware(RequestIDMiddleware)

# CORS origins берутся из settings.ALLOWED_ORIGINS. В .env передавать как
# JSON-массив (pydantic умеет парсить):
#   ALLOWED_ORIGINS=["https://grafana.example.com","https://app.example.com"]
# Default settings.ALLOWED_ORIGINS = ["*"] — допустимо только для dev.
allowed_origins = settings.ALLOWED_ORIGINS
if settings.ENV == "production" and allowed_origins == ["*"]:
    raise RuntimeError(
        "ALLOWED_ORIGINS=['*'] is forbidden in production. "
        "Set a concrete origin list in env/secrets."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Redis-backed sliding window — общий счётчик между api-репликами.
    # См. app/api/rate_limit.py. Fail-open: при недоступности Redis запрос
    # пропускается с warning-логом (auth остаётся на HMAC-подписи).
    if request.url.path == "/webhooks/alertmanager":
        client_ip = request.client.host if request.client else ""
        if not await rate_limit.check_alertmanager(client_ip):
            return Response(status_code=429, content="Rate limit exceeded")
    return await call_next(request)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    observe_request_latency(time.time() - start_time)
    return response


app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(approvals.router, prefix="/approvals", tags=["approvals"])
app.include_router(replay.router, prefix="/replay", tags=["replay"])
app.include_router(feedback.router, prefix="/evaluation", tags=["evaluation"])
app.include_router(discord_interactions.router, prefix="/discord", tags=["discord"])


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
