import asyncio
import json
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

from app.agents.analyzer import AnalyzerAgent
from app.config import settings
from app.context.context_builder import ContextBuilder
from app.core.state_machine import IncidentState, StateMachine
from app.database import SessionLocal
from app.models import Conversation
from app.replay.contract import assert_replay_isolated_runtime
from app.services.resilience import LLMResilienceManager, LoopLocalRedis
from app.services.session_manager import SessionManager

# Единое Celery-приложение. Раньше здесь жил отдельный Celery("worker") без
# backpressure-конфигурации — на общем Redis-broker/backend это давало ДВА
# app ("worker" + "sre_tasks") и пользовательский /copilot-трафик шёл в
# незащищённый app (нет max_tasks_per_child/prefetch/soft-time-limit, риск
# OOM/перегрузки/висящих задач + коллизия result-ключей на общем backend).
# Теперь `generate_reply` регистрируется на том же sre_tasks-app, что и
# incident-pipeline, и наследует ПОЛНЫЙ backpressure-конфиг из workers.tasks.
# `celery_app` остаётся реэкспортом для обратной совместимости импортов
# (app/main.py, app/api/replay.py и тесты по-прежнему берут его отсюда).
from app.workers.tasks import celery_app

# Initialize services
logger = structlog.get_logger()
# LoopLocalRedis вместо голого from_url: Celery-таски выполняются через
# `asyncio.run(...)` (новый event loop на задачу), а процесс живёт до 50 задач
# (worker_max_tasks_per_child). Модульный клиент с одним connection pool
# привязывал коннекты к loop-у ПЕРВОЙ задачи — все последующие получали
# "Event loop is closed"/"attached to a different loop", из-за чего circuit
# breaker и session-хранилище молча деградировали в no-op. Прокси выдаёт
# отдельный клиент per-loop (и общий fallback для sync-контекста); публичное
# имя `redis_client` сохранено — его импортируют app/api/approvals.py и
# app/services/discord/embed_builder.py.
redis_client = LoopLocalRedis(settings.REDIS_URL)
# LoopLocalRedis — прозрачный прокси: любой атрибут делегируется реальному
# redis.asyncio.Redis, выбранному под текущий event loop. Для потребителей это
# неотличимо от Redis, но статически это разные типы — сужаем на границе.
_redis: "Redis" = cast("Redis", redis_client)
resilience = LLMResilienceManager(_redis)
session_manager = SessionManager(_redis)


async def _generate_reply_logic(
    conversation_id: str,
    prompt: str,
    replay_mode: bool = False,
    snapshot: dict | None = None,
    environment_fingerprint: str | None = None,
) -> str:
    db = SessionLocal()
    conv = db.query(Conversation).filter_by(id=conversation_id).first()
    if conv is None:
        raise ValueError(f"Conversation {conversation_id!r} not found")

    def transition(to_state: IncidentState):
        if not StateMachine.validate_transition(
            IncidentState(conv.current_state), to_state
        ):
            # В режиме replay игнорируем ошибки переходов для гибкости
            if not replay_mode:
                raise Exception(
                    f"Invalid transition from {conv.current_state} to {to_state}"
                )
        conv.current_state = to_state.value
        db.commit()

    try:
        transition(IncidentState.INVESTIGATING)
        if replay_mode:
            assert_replay_isolated_runtime(
                allow_network_egress=False,
                allow_k8s_api=False,
                allow_external_tools=False,
            )
            if not snapshot:
                raise Exception("Replay mode requires immutable snapshot input")
            enriched_ctx = snapshot.get("payload", {})
        else:
            builder = ContextBuilder()
            # legacy /copilot-путь: Conversation-модель не имеет поля data
            # (см. app/models/__init__.py); защищаемся через getattr,
            # эта ветка не используется основным incident-pipeline-ом.
            enriched_ctx = await builder.build_context(getattr(conv, "data", {}))

        # Reasoning Loop
        for iteration in range(3):
            analyzer = AnalyzerAgent()
            analysis_data = await analyzer.analyze(enriched_ctx)

            try:
                analysis = json.loads(analysis_data)
                confidence = analysis.get("confidence_score", 0)
            except json.JSONDecodeError as e:
                logger.error(
                    "llm_response_parse_error", response=analysis_data, error=str(e)
                )
                confidence = 0

            if confidence >= 0.7:
                transition(IncidentState.HYPOTHESIS_GENERATED)
                break
            else:
                logger.warning(
                    "low_confidence_loop", iteration=iteration, score=confidence
                )
                enriched_ctx["socratic_feedback"] = (
                    "Confidence too low. Focus on specific pod logs."
                )
        else:
            raise Exception("Failed to reach confidence threshold after 3 iterations")

        transition(IncidentState.FIX_PROPOSED)

        # Legacy Discord notification удалён: основной отчёт идёт через
        # app/workers/pipeline.py::send_incident_report (severity-gated embed).
        # См. PR chore/gc-legacy-discord-senders.

        if replay_mode and environment_fingerprint:
            return json.dumps(
                {
                    "analysis": analysis_data,
                    "environment_fingerprint": environment_fingerprint,
                }
            )
        return analysis_data

    except Exception as e:
        if conv:
            conv.current_state = IncidentState.FAILED.value
            db.commit()
        logger.error("celery_task_failed", error=str(e))
        raise
    finally:
        db.close()


@celery_app.task(name="generate_reply", bind=True, max_retries=3)
def generate_reply(
    self,
    conversation_id: str,
    prompt: str,
    replay_mode: bool = False,
    snapshot: dict | None = None,
    environment_fingerprint: str | None = None,
):
    try:
        return asyncio.run(
            _generate_reply_logic(
                conversation_id, prompt, replay_mode, snapshot, environment_fingerprint
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2**self.request.retries)
