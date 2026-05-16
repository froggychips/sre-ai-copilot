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

# Backpressure-настройки (см. config.py для пояснений).
# Не применяются в eager-mode чтобы тесты не упирались в time-limit'ы.
if not settings.CELERY_TASK_ALWAYS_EAGER:
    celery_app.conf.update(
        worker_prefetch_multiplier=settings.CELERY_WORKER_PREFETCH_MULTIPLIER,
        worker_max_tasks_per_child=settings.CELERY_WORKER_MAX_TASKS_PER_CHILD,
        task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,
        task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS,
        # Celery 6 deprecation — без этого warning на старте worker'а.
        broker_connection_retry_on_startup=True,
    )

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
    # TC deploys → kg_deployments. БЕЗ LLM. Включает RecentDeployRule
    # на live deploys без incident-flow. Каждые 15 минут берёт recent
    # builds из TC за 24h и upsert-ит в kg_deployments по (service, sha).
    "tc-deploys-to-kg": {
        "task": "tc_deploys_to_kg",
        "schedule": crontab(minute="*/15"),
    },
    # L5 (alert-fatigue): chronic-alerts digest в канал #stats каждые 6h.
    # Гасит mute-эффект от suppress-chronic — даёт видимость сервисов
    # которые тлеют под suppress. Управляется CHRONIC_DIGEST_ENABLED.
    "chronic-alerts-digest": {
        "task": "chronic_alerts_digest",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # A4: k8s pod-events → kg_pod_events. Каждые 10 мин тянем Warning-events
    # из всех ns с deployments в KG. Idempotent по event_uid. Источник
    # OOMKilled / FailedScheduling / ImagePullBackOff / Unhealthy / etc.
    "k8s-pod-events-sync": {
        "task": "k8s_pod_events_sync",
        "schedule": crontab(minute="*/10"),
    },
    # D2-auto: drift cleanup. Раз в час пересинхронизирует kg_services со
    # списком существующих в k8s ns. Safety threshold 20% — защита от
    # mass-mark при временной недоступности API server.
    "kg-drift-cleanup": {
        "task": "kg_drift_cleanup",
        "schedule": crontab(minute=17),  # ежечасно в 17 мин
    },
}


@celery_app.task(
    name="process_incident",
    bind=True,
    max_retries=3,
    rate_limit=settings.CELERY_PROCESS_INCIDENT_RATE_LIMIT,
)
def process_incident_task(self, incident_data: dict):
    # Hard-gate проверяется внутри `async_process_incident` чтобы закрыть
    # оба entry-point (Celery task + PIPELINE_DIRECT_INVOKE из webhook).
    # asyncio.run создаёт чистый event loop на задачу — get_event_loop()
    # на Python 3.10+ выкидывает DeprecationWarning, а в Python 3.14+
    # без running loop возвращает ошибку. Celery worker — синхронный
    # context, eager-режим (тесты) тоже работает корректно с asyncio.run.
    return asyncio.run(async_process_incident(incident_data))


async def async_process_incident(incident_data: dict):
    incident_id = incident_data.get("incident_id", "")
    _service = (incident_data.get("labels") or {}).get("service", "")
    _namespace = incident_data.get("namespace", "")

    # ── HARD GATE: защита от случайного LLM-burn ───────────────────────
    # См. settings.LLM_PIPELINE_ENABLED. Default=False закрывает оба
    # entry-point: Celery task + PIPELINE_DIRECT_INVOKE из webhook.
    # Audit-event обнаруживает unintended-routing на дашборде.
    if not settings.LLM_PIPELINE_ENABLED:
        logger.warning(
            "pipeline.skipped_disabled incident_id=%s ns=%s",
            incident_id, _namespace,
        )
        audit_service.log_event("PIPELINE_DISABLED_SKIP", {
            "incident_id": incident_id,
            "namespace": _namespace,
            "reason": "LLM_PIPELINE_ENABLED=false",
        })
        return {"status": "skipped", "reason": "LLM_PIPELINE_ENABLED=false"}

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


@celery_app.task(name="k8s_pod_events_sync")
def k8s_pod_events_sync_task():
    """A4: pull Warning k8s-events → kg_pod_events. БЕЗ LLM."""
    from app.knowledge_graph.k8s_events_sync import sync_all_events

    db = SessionLocal()
    try:
        result = sync_all_events(db)
    finally:
        db.close()
    return result


@celery_app.task(name="kg_drift_cleanup")
def kg_drift_cleanup_task():
    """D2-auto: пометить services из несуществующих ns как synthetic.

    Safety: max_drift_pct=20% — при kubectl-failure / временной недоступности
    API server вернётся пустой ns-set, drift станет 100%, threshold заблокирует
    UPDATE. Manual run для override: `python -m app.scripts.cleanup_drift --apply`.
    """
    from app.knowledge_graph.drift_cleanup import run_drift_cleanup

    db = SessionLocal()
    try:
        return run_drift_cleanup(db, max_drift_pct=20.0, apply=True)
    except Exception as e:
        logger.warning("kg_drift_cleanup.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="tc_deploys_to_kg")
def tc_deploys_to_kg_task():
    """Pull recent TC deploys → upsert в kg_deployments. БЕЗ LLM-вызовов.

    Stage 1: per-namespace attribution. Для каждого TC build с branch
    соответствующим namespace берётся «ns-representative» service (первый
    non-synthetic в этом ns) и пишется deployment-record. Это даёт
    `RecentDeployRule` работающий сигнал «deploy в этом ns был N минут назад»
    хотя и не per-service.

    Per-service attribution через `TC.changes.files` — отдельная Stage 2.

    Dedup: `record_deployment` идемпотентен по (service_id, buildtype_id,
    build_number) — повторные cron-tick'и не плодят дубликаты.
    """
    return asyncio.run(_tc_deploys_to_kg_logic())


async def _tc_deploys_to_kg_logic() -> dict:
    from datetime import datetime, timezone

    from app.knowledge_graph.populator import record_deployment
    from app.knowledge_graph.schema import Service
    from app.services.teamcity_service import branch_for_namespace, recent_deploys

    # Берём builds за последние 24h. Cron каждые 15 мин — overkill по
    # window, но dedup защищает и cheaply tolerant к беатам-пропускам.
    builds = await recent_deploys(lookback_hours=24, limit=200)
    if not builds:
        return {"builds_fetched": 0, "kg_deployments_added": 0}

    db = SessionLocal()
    added = 0
    try:
        # Map branch → list of namespaces в KG. Реализуется через обратное
        # отображение `branch_for_namespace`: проходим distinct namespaces
        # из kg_services и группируем по branch.
        all_ns = [
            ns for (ns,) in db.query(Service.namespace).distinct().all()
        ]
        ns_by_branch: dict[str, list[str]] = {}
        for ns in all_ns:
            br = branch_for_namespace(ns)
            if br:
                ns_by_branch.setdefault(br, []).append(ns)

        for b in builds:
            branch_full = (b.get("branch") or "").replace("refs/heads/", "")
            target_namespaces = ns_by_branch.get(branch_full, [])
            if not target_namespaces:
                continue

            finished = b.get("finished_at")
            # startDate из TC (A3 fix) — без него RecentDeployRule матчит окно
            # по finishDate и пропускает alert'ы, прилетевшие пока деплой ещё
            # идёт. Fallback на finished_at — для совместимости со старыми
            # builds, где startDate не приехал.
            started = b.get("started_at") or b.get("finished_at")
            if not started:
                continue
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                continue
            started_naive = started_dt.replace(tzinfo=None)
            finished_naive = None
            if finished:
                try:
                    finished_naive = datetime.fromisoformat(
                        finished.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            for ns in target_namespaces:
                # A3: ns-wide broadcast. Раньше писали только на «ns-representative»
                # (первый non-synthetic) — из-за этого 98% сервисов в KG не
                # имели deploy-истории. RecentDeployRule работает per-service,
                # значит каждый сервис в ns должен видеть тот же deploy event.
                # Dedup защищён уникальностью (service_id, buildtype_id, build_number).
                ns_services = (
                    db.query(Service)
                    .filter_by(namespace=ns, synthetic=False)
                    .all()
                )
                for svc in ns_services:
                    try:
                        record_deployment(
                            db,
                            service=svc,
                            started_at=started_naive,
                            finished_at=finished_naive,
                            buildtype_id=b.get("buildtype_id"),
                            build_number=str(b.get("number") or ""),
                            status=b.get("status"),
                            triggered_by=b.get("triggered_by"),
                            extras={
                                "branch": branch_full,
                                "buildtype_name": b.get("buildtype_name"),
                                "url": b.get("url"),
                                "namespace_scope": True,  # маркер ns-wide attribution
                            },
                        )
                        added += 1
                    except Exception as e:
                        logger.warning(
                            "tc_deploys_to_kg.record_failed ns=%s svc=%s build=#%s: %s",
                            ns, svc.name, b.get("number"), e,
                        )
        db.commit()
    except Exception as e:
        logger.error("tc_deploys_to_kg.failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()

    now_unix = datetime.now(timezone.utc).timestamp()
    logger.info(
        "tc_deploys_to_kg.done builds=%d added=%d at=%.0f",
        len(builds), added, now_unix,
    )
    return {"builds_fetched": len(builds), "kg_deployments_added": added}


@celery_app.task(name="chronic_alerts_digest")
def chronic_alerts_digest_task():
    """L5: список «хронически тлеющих» сервисов в канал #stats.

    Гасит mute-эффект от L2 suppress-chronic. БЕЗ LLM — простой
    SQL-aggregate по kg_alerts + markdown через send_stats_report.
    """
    from app.services.chronic_digest import send_chronic_digest

    db = SessionLocal()
    try:
        result = asyncio.run(send_chronic_digest(db))
        logger.info("chronic_alerts_digest.done result=%s", result)
        return result
    except Exception as e:
        logger.error("chronic_alerts_digest.failed: %s", e)
        return {"status": "error", "error": str(e)}
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
