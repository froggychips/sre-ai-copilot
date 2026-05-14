import asyncio
import logging

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.core.state_machine import IncidentState
from app.database import IncidentRecord, SessionLocal
from app.services.audit_logger import audit_service
from app.services.telemetry_utils import incident_span
from app.telemetry import setup_telemetry
from app.workers.pipeline import IncidentPipeline, transition_to

setup_telemetry(service_name="copilot-worker")

logger = logging.getLogger(__name__)

celery_app = Celery("sre_tasks", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

if settings.CELERY_TASK_ALWAYS_EAGER:
    # Inline-режим для локального e2e: process_incident_task.delay(...)
    # выполняется синхронно в текущем процессе, без Redis/worker-а.
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True

celery_app.conf.beat_schedule = {
    "kg-topology-sync": {
        "task": "kg_topology_sync",
        "schedule": crontab(minute=0),  # каждый час
    },
    # Daily stats digest (включается через STATS_DIGEST_ENABLED).
    # БЕЗ LLM-вызовов — pure aggregation, шлёт в DISCORD_WEBHOOK_STATS_URL.
    "daily-stats-digest": {
        "task": "daily_stats_digest",
        "schedule": crontab(hour=settings.STATS_DIGEST_HOUR_UTC, minute=0),
    },
}


@celery_app.task(name="process_incident", bind=True, max_retries=3)
def process_incident_task(self, incident_data: dict):
    # asyncio.run создаёт чистый event loop на задачу — get_event_loop()
    # на Python 3.10+ выкидывает DeprecationWarning, а в Python 3.14+
    # без running loop возвращает ошибку. Celery worker — синхронный
    # context, eager-режим (тесты) тоже работает корректно с asyncio.run.
    return asyncio.run(async_process_incident(incident_data))


async def async_process_incident(incident_data: dict):
    incident_id = incident_data.get("incident_id", "")
    _service = (incident_data.get("labels") or {}).get("service", "")
    _namespace = incident_data.get("namespace", "")

    with incident_span(incident_id, service=_service, namespace=_namespace) as _root_span:
        db = SessionLocal()
        audit_service.log_event("CELERY_START", {"incident_id": incident_id})
        record = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident_id)
            .first()
        )
        try:
            pipeline = IncidentPipeline(incident_data, db, record, _root_span)
            await pipeline.run()
        except Exception as e:
            if record is not None:
                try:
                    transition_to(record, IncidentState.FAILED, db)
                except ValueError:
                    db.rollback()
            else:
                db.rollback()
            raise e
        finally:
            db.close()


@celery_app.task(name="kg_topology_sync")
def kg_topology_sync_task():
    from app.knowledge_graph.kg_sync import sync_topology

    db = SessionLocal()
    try:
        result = sync_topology(db)
        logger.info("kg_topology_sync.done result=%s", result)
        return result
    except Exception as e:
        logger.error("kg_topology_sync.failed: %s", e)
        raise
    finally:
        db.close()


@celery_app.task(name="daily_stats_digest")
def daily_stats_digest_task():
    """Daily Discord-stats report. Pure data-aggregation, БЕЗ LLM-вызовов.

    Управляется флагом settings.STATS_DIGEST_ENABLED. Если False —
    задача всё равно запускается по расписанию (контракт Celery beat),
    но внутри `send_daily_digest()` сразу возвращает skipped.
    """
    from app.services.stats_digest import send_daily_digest

    db = SessionLocal()
    try:
        result = asyncio.run(send_daily_digest(db))
        logger.info("daily_stats_digest.done result=%s", result)
        return result
    except Exception as e:
        logger.error("daily_stats_digest.failed: %s", e)
        # Не падаем в Celery (retry бесполезен, это аналитический task).
        return {"status": "error", "error": str(e)}
    finally:
        db.close()
