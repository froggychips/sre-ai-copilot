import time
from contextlib import asynccontextmanager
from typing import Optional
from uuid import UUID

import structlog
from celery.result import AsyncResult
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import start_http_server
from sqlalchemy import text

from app import repository
from app.api import approvals, discord_interactions, rate_limit, replay, webhooks
from app.evaluation import feedback
from app.auth import User, get_current_user
from app.celery_worker import celery_app, generate_reply, redis_client, resilience
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
    # addr берётся из settings.METRICS_BIND_ADDR (default "0.0.0.0", чтобы
    # in-cluster scraping продолжал работать). В PROD порт :8001 ОБЯЗАТЕЛЬНО
    # ограничивать через NetworkPolicy — иначе метрики доступны всему поду/сети.
    start_http_server(port=8001, addr=settings.METRICS_BIND_ADDR)
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
# Проверка через settings.is_production, а не литерал ENV == "production": деплой
# с ENV=prod тихо обходил и этот гвард, и CORS-гвард ниже (тот же класс бага,
# что чинили в fail-closed вебхуке — см. app/api/webhooks.py).
if settings.is_production:
    app = FastAPI(
        title="SRE AI Copilot",
        version="1.0.0-rc.3",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
else:
    app = FastAPI(title="SRE AI Copilot", version="1.0.0-rc.3", lifespan=lifespan)
setup_telemetry(app)

app.add_middleware(RequestIDMiddleware)

# CORS origins берутся из settings.ALLOWED_ORIGINS. В .env передавать как
# JSON-массив (pydantic умеет парсить):
#   ALLOWED_ORIGINS=["https://grafana.example.com","https://app.example.com"]
# Default settings.ALLOWED_ORIGINS = ["*"] — допустимо только для dev.
#
# Гвард по ЧЛЕНСТВУ "*", не по равенству списка: Starlette включает
# wildcard-режим через `"*" in allow_origins`, так что `["*", "https://x"]`
# проходил бы equality-гвард И включал wildcard.
allowed_origins = settings.ALLOWED_ORIGINS
wildcard_origins = "*" in allowed_origins
if settings.is_production and wildcard_origins:
    raise RuntimeError(
        "Wildcard '*' in ALLOWED_ORIGINS is forbidden in production. "
        "Set a concrete origin list in env/secrets."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    # Wildcard + credentials несовместимы: в этой комбинации Starlette
    # эхом возвращает ЛЮБОЙ Origin атакующего вместе с
    # Access-Control-Allow-Credentials: true. При wildcard гасим credentials.
    allow_credentials=not wildcard_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


def _rate_limit_client_ip(request: Request) -> str:
    """Ключ rate-limit-бакета: реальный клиентский IP.

    uvicorn работает без --proxy-headers, поэтому за ingress'ом
    request.client.host — это IP прокси: ВЕСЬ трафик делил один бакет
    (10 req/min на кластер — alert-шторм сам себя рейт-лимитил). Если
    оператор ЯВНО задал TRUSTED_PROXY_HEADER (например "X-Forwarded-For"),
    берём IP оттуда — ПОСЛЕДНИЙ элемент списка (его дописал ближайший
    доверенный прокси; левые элементы клиент может подделать). Без явной
    конфигурации заголовкам не доверяем — fallback на client.host.
    """
    header_name = getattr(settings, "TRUSTED_PROXY_HEADER", None)
    if header_name:
        raw = request.headers.get(header_name, "")
        if raw:
            candidate = raw.split(",")[-1].strip()
            if candidate:
                return candidate
    return request.client.host if request.client else ""


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Redis-backed sliding window — общий счётчик между api-репликами.
    # См. app/api/rate_limit.py. Fail-open: при недоступности Redis запрос
    # пропускается с warning-логом (auth остаётся на HMAC-подписи).
    if request.url.path.startswith("/webhooks/alertmanager"):
        client_ip = _rate_limit_client_ip(request)
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
app.include_router(
    replay.router,
    prefix="/replay",
    tags=["replay"],
    dependencies=[Depends(get_current_user)],
)
app.include_router(feedback.router, prefix="/evaluation", tags=["evaluation"])
app.include_router(discord_interactions.router, prefix="/discord", tags=["discord"])


@app.post("/copilot", status_code=202)
async def post_copilot(
    response: Response,
    conversation_id: Optional[UUID] = Body(None),
    prompt: str = Body(...),
    user: User = Depends(get_current_user),
):
    # Per-user rate limit (resilience.py token-bucket). /copilot — user-facing
    # точка входа, ровно под что check_rate_limit(user_id) и спроектирован
    # (НЕ автономный alert-пайплайн, который этот бакет задушил бы на fan-out).
    # Fail-open: redis недоступен → пропускаем, как rate_limit_middleware на вебхуке.
    try:
        allowed = await resilience.check_rate_limit(user.sub)
    except Exception:
        allowed = True
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Диалог принадлежит пользователю (JWT-claim `sub`). Раньше владельца не
    # было вовсе и `conversation_id` из тела принимался как есть: любой
    # аутентифицированный пользователь мог дописать сообщение в ЧУЖОЙ диалог,
    # сдвинуть его state machine через generate_reply и забрать результат
    # через /jobs/{task_id}.
    #
    # Чужой и несуществующий conversation_id дают ОДИНАКОВЫЙ 404 (не 403):
    # иначе ручка становится оракулом «такой диалог существует» для перебора
    # UUID-ов. Проверка живёт внутри add_message — в одной сессии с записью и
    # ДО постановки задачи в Celery, так что при отказе задача не ставится.
    if conversation_id is None:
        conversation_id = await repository.create_conversation(owner_sub=user.sub)
    await repository.add_message(
        conv_id=conversation_id,
        role=MessageRole.user,
        content=prompt,
        owner_sub=user.sub,
    )
    # .delay() — синхронный publish в Redis-брокер (blocking socket I/O).
    # В async-ручке такой вызов при деградации брокера морозит весь event
    # loop (механика инцидента 08.08: liveness перестаёт отвечать → kubelet
    # убивает api-поды) — уводим в threadpool.
    task = await run_in_threadpool(generate_reply.delay, str(conversation_id), prompt)
    # Владелец задачи — единственная связь «task_id → пользователь» (см. блок
    # про /jobs ниже): без неё результат разбора отдавался любому
    # аутентифицированному, кто увидел task_id в Location-заголовке или в логах.
    await _remember_job_owner(task.id, user.sub)
    response.headers["Location"] = f"/jobs/{task.id}"
    return {"task_id": task.id, "conversation_id": conversation_id}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


def _check_db_ready() -> None:
    # engine — синхронный SQLAlchemy Engine: connect() при деградации PG
    # блокируется до connect_timeout. Вызывается ТОЛЬКО через threadpool.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@app.get("/readyz")
async def readyz():
    try:
        # Sync-пробу БД уводим в threadpool: инлайновый engine.connect()
        # в async-ручке при зависшем PG морозил event loop целиком —
        # /healthz переставал отвечать и kubelet убивал api-поды
        # (механика инцидента 08.08).
        await run_in_threadpool(_check_db_ready)
        return {"status": "ready"}
    except Exception as e:
        # Не светим детали исключения наружу — логируем server-side,
        # клиенту отдаём статичный detail.
        log.warning("readyz_db_unreachable", error=str(e))
        raise HTTPException(status_code=503, detail="database unavailable")


# ── /jobs: владелец задачи ───────────────────────────────────────────────
#
# Celery-задача сама по себе владельца не знает, а `/jobs/{task_id}` до этого
# фикса отдавала РЕЗУЛЬТАТ любой задачи любому аутентифицированному
# пользователю. Держалось это на том, что task_id — недогадываемый UUID, то
# есть фактически capability-token. Но этот «токен» светится в
# Location-заголовке ответа /copilot, в теле ответа, в access-логах ingress-а и
# в логах приложения, а сам результат — LLM-разбор ЧУЖОГО диалога (тот же
# класс IDOR, что чинили в /copilot через Conversation.owner_sub).
#
# Однозначной связи «celery task → пользователь» в данных нет: task_id
# генерирует брокер, в Conversation он не пишется, а Celery-результат хранит
# только payload задачи. Поэтому владелец фиксируется ЯВНО в момент постановки
# задачи и сверяется при чтении.
#
# Хранилище — Redis (тот же, что celery result backend): запись обязана быть
# видна ВСЕМ api-репликам, иначе поллинг сломается при 2 подах (поставил на
# реплике A, спрашиваешь у B). TTL совпадает с дефолтным celery
# `result_expires` (24ч) — дольше запись бессмысленна, результата уже нет.
#
# Fail-closed: нет записи о владельце (Redis недоступен/ключ протух/задача
# поставлена ДРУГИМ путём) → 404, тот же ответ, что на несуществующий task_id.
# Это анти-оракул (как 404 в /copilot и /approvals), но у этого есть цена:
# `/jobs` обслуживает ТОЛЬКО copilot-задачи, что и записано в контракте
# (docs/SEMANTIC_CONTRACT.md). Статус задач других путей (например
# `POST /replay/{incident_id}`) смотрится через `GET /webhooks/status/{task_id}`.
_JOB_OWNER_KEY_PREFIX = "job_owner:"
_JOB_OWNER_TTL_SECONDS = 24 * 3600
_JOB_NOT_FOUND = "Job not found"


async def _remember_job_owner(task_id: str, owner_sub: str) -> None:
    """Записать владельца свежепоставленной задачи. Best-effort.

    NX — чтобы уже привязанную задачу нельзя было перепривязать на другого
    владельца. Ошибку Redis не превращаем в ошибку постановки: задача уже в
    брокере и будет выполнена, просто её результат нельзя будет прочитать
    через /jobs (fail-closed, см. _job_owner_matches).
    """
    if not task_id or not owner_sub:
        return
    try:
        await redis_client.set(
            f"{_JOB_OWNER_KEY_PREFIX}{task_id}",
            owner_sub,
            ex=_JOB_OWNER_TTL_SECONDS,
            nx=True,
        )
    except Exception as e:
        log.warning("job_owner_not_recorded", task_id=task_id, error=str(e))


async def _job_owner_matches(task_id: str, owner_sub: str) -> bool:
    """True только если задача явно принадлежит `owner_sub`.

    Всё остальное (нет записи, чужой владелец, Redis недоступен) — False:
    отказ, неотличимый от «такой задачи нет».
    """
    if not owner_sub:
        return False
    try:
        stored = await redis_client.get(f"{_JOB_OWNER_KEY_PREFIX}{task_id}")
    except Exception as e:
        log.warning("job_owner_lookup_failed", task_id=task_id, error=str(e))
        return False
    if isinstance(stored, bytes):
        # decode_responses у клиента не задан централизованно — терпим оба вида.
        stored = stored.decode("utf-8", "replace")
    return bool(stored) and stored == owner_sub


def _job_status_snapshot(task_id: str) -> dict:
    # AsyncResult.status/.ready()/.result — синхронные обращения к Redis
    # result-backend (blocking socket I/O). Вызывается ТОЛЬКО через
    # threadpool, чтобы деградация Redis не морозила event loop.
    result = AsyncResult(task_id, app=celery_app)
    response_data = {"task_id": task_id, "status": result.status}

    if result.ready():
        if result.successful():
            response_data["result"] = result.result
        else:
            # Не отдаём наружу детали ошибки задачи (могут содержать
            # внутренние сообщения/traceback) — логируем server-side.
            log.warning("job_failed", task_id=task_id, error=str(result.result))
            response_data["error"] = "task failed"

    return response_data


@app.get("/jobs/{task_id}")
async def get_job_status(
    task_id: str,
    user: User = Depends(get_current_user),
):
    # Ownership ДО обращения к result-backend: чужая/неизвестная задача не
    # должна даже провоцировать запрос в Redis, а ответ обязан быть одинаковым
    # в обоих случаях (иначе ручка — оракул «такой task_id существует»).
    if not await _job_owner_matches(task_id, user.sub):
        log.warning(
            "job_access_denied",
            task_id=task_id,
            requested_by=user.sub,
        )
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    return await run_in_threadpool(_job_status_snapshot, task_id)
