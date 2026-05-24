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
    # Точка роста #2 (Phase 2): AlertEvent resolve sync. Каждые 15 мин
    # сравниваем kg_alerts.fingerprint с активными на AM, не-firing
    # помечаем resolved_at=NOW. Без этого stale firing alerts копятся
    # годами (см. etcdMembersDown от 10 апреля).
    "kg-alerts-resolve-sync": {
        "task": "kg_alerts_resolve_sync",
        "schedule": crontab(minute="*/15"),
    },
    # Phase 3-B: k8s Ingress → external entrypoint edges. Раз в час
    # синхронизирует Ingress resources cluster-wide. Создаёт synthetic-узлы
    # `ingress:<host>` + edges на backend services. Это первый источник
    # «откуда приходит трафик» вне cluster-internal env-scan.
    "kg-ingress-sync": {
        "task": "kg_ingress_sync",
        "schedule": crontab(minute=37),  # ежечасно в 37 мин (offset от других)
    },
    # ChatGPT review #4.3: service health composite (open alerts × severity
    # + chronic pod events + recurrence). Раз в 20 мин — health моментальный
    # сигнал, но recompute дорого над всеми ~370 real services. Используется
    # в kg_fragile_top для «истинного» ранжирования и в digest «🩺 Unhealthy».
    "kg-health-recompute": {
        "task": "kg_health_recompute",
        "schedule": crontab(minute="*/20"),
    },
    # External probe: DNS+TCP+HTTPS на synthetic `ingress:<host>` узлы. Каждую
    # минуту проверяет публичные endpoint'ы (источник — k8s Ingress hosts из
    # kg_ingress_sync). При consecutive_failures ≥ EXTERNAL_PROBE_FAIL_THRESHOLD
    # шлёт embed в DISCORD_WEBHOOK_URL и пишет AlertEvent в kg_alerts.
    # Default OFF (EXTERNAL_PROBE_ENABLED=False) — включается осознанно после
    # подгонки threshold/таргетов.
    "kg-external-probe": {
        "task": "kg_external_probe",
        "schedule": crontab(minute="*"),
    },
    # Метрические сигналы (2026-05-22, миграция 20260522_0100):
    # 4 новых таски материализуют time-series из VictoriaMetrics в KG.
    # До них VM-клиент использовался on-demand в pipeline/stats_digest и
    # ничего исторического не сохранялось.
    #
    # per-service snapshot cpu/mem/restarts/5xx/p95. UNIQUE(service_id, ts) →
    # idempotent. Если VICTORIA_METRICS_URL пустой — task no-op.
    "kg-metrics-sync": {
        "task": "kg_metrics_sync",
        "schedule": crontab(minute="*/10"),
    },
    # Global cluster snapshot — те же поля что в #stats daily report,
    # но раз в 5 мин и материализованно. Используется для trend-аналитики
    # и post-mortem контекста.
    "kg-cluster-health-sync": {
        "task": "kg_cluster_health_sync",
        "schedule": crontab(minute="*/5"),
    },
    # Per ingress endpoint observations (p95/p99/rps/4xx/5xx) из nginx-ingress
    # exporter. host/path берутся из k8s Ingress resources (kubectl get -A).
    "kg-ingress-observations-sync": {
        "task": "kg_ingress_observations_sync",
        "schedule": crontab(minute="*/10"),
    },
    # Pre-compute per-service агрегаты сигналов из САМОГО KG (deploys/alerts/
    # pod_events) за 24h окно. Hourly. Не ходит в VM — pure SQL.
    "kg-signal-aggregates-compute": {
        "task": "kg_signal_aggregates_compute",
        "schedule": crontab(minute=23),  # ежечасно в 23 мин (offset от drift/ingress)
    },
    # Anomaly detection (2026-05-22, миграция 20260522_0200):
    # rolling z-score по kg_service_health для каждой из 5 метрик.
    # При |z|>3 пишем в kg_anomaly_observations (severity warning/critical).
    # Идемпотентно по (service_id, ts, metric). Discord-уведомление — фаза 2.
    "kg-anomaly-detection-task": {
        "task": "kg_anomaly_detection_task",
        "schedule": crontab(minute="*/10"),
    },
    # Runtime correlation: подтверждает existing edges через co-occurrence
    # warning-событий (BackOff/Unhealthy/OOMKilled/...) у src+dst в окне 15 мин.
    # Sliding window 7 дней — дорогой запрос, /30 мин достаточно.
    # Управляется RUNTIME_CORRELATION_ENABLED.
    "kg-runtime-correlation-sync": {
        "task": "kg_runtime_correlation_sync",
        "schedule": crontab(minute="*/30"),
    },
    # Per-team daily digest — один embed per team_owner (squad-N / infra /
    # monitoring). Зависит от kg_signal_aggregates (slo_burn_pct) и
    # kg_services.health_score — должен запускаться ПОСЛЕ их compute.
    # Управляется TEAM_DIGEST_ENABLED.
    "team-daily-digest": {
        "task": "team_daily_digest",
        "schedule": crontab(hour=settings.TEAM_DIGEST_HOUR_UTC, minute=0),
    },
    # Error/Fatal логи из Seq → kg_log_observations. Тянет по настроенным
    # Seq-инстансам (prod/preprod/preupdate) count событий за окно ~10 мин,
    # агрегирует per service per level и пишет одну строку через
    # ON CONFLICT DO UPDATE. Если SEQ_* пусто — no-op.
    "kg-seq-logs-sync-task": {
        "task": "kg_seq_logs_sync",
        "schedule": crontab(minute="*/10"),
    },
    # KG self-health canary (Wave 5 retrospective): «monitoring of the
    # monitoring». Ищет тихие деградации в наших же KG-таблицах
    # (mem_pct=0 за неделю, sync_lag, stale alerts, и т.п.). На FAIL —
    # audit-log + опциональный Discord embed в DISCORD_WEBHOOK_SELF_HEALTH_URL
    # (отдельный dev-канал, не #infra-error). Idempotency: 6h dedup window.
    "kg-self-health-check": {
        "task": "kg_self_health_check",
        "schedule": crontab(minute="*/30"),
    },
    # Stuck-alerts escalation (KG TTR-analytics, 2026-05-23): alerts firing
    # >24h без resolved_at теряются в потоке свежих firing-event'ов. Hourly
    # — длинная firing-window не меняется быстро, чаще нет смысла. Audit-log
    # + опциональный Discord embed в DISCORD_WEBHOOK_STUCK_ALERTS_URL
    # (dedicated канал, не #infra-error). Idempotency: 6h dedup window
    # по set fingerprint stuck-alert-id-ов.
    "kg-stuck-alerts-check": {
        "task": "kg_stuck_alerts_check",
        "schedule": crontab(minute=11),  # ежечасно в 11 мин (offset от drift=17/ingress=37)
    },
    # Wave 7 / G1.3: declarative parser k8s Service + Ingress resources.
    # Каждые 15 мин получает все Services и Ingresses cluster-wide,
    # upsert kg_services (с k8s_service/k8s_ingress metadata) + edges
    # `serves_traffic` (Service → backing Deployment по selector) и
    # `routes_to` (Ingress → backend Service). Это самый дешёвый
    # declarative источник топологии — снимает с env-scan'а часть
    # нагрузки. См. k8s_topology_resources_sync.py.
    "kg-topology-resources-sync": {
        "task": "kg_topology_resources_sync",
        "schedule": crontab(minute="*/15"),
    },
    # KG Coverage #1: k8s Job + CronJob → kg_k8s_jobs. Каждые 15 мин
    # `kubectl get jobs,cronjobs -A` + upsert. Сигналы: last_successful_time
    # / last_schedule_time / failed_count / last_pod_exit_code. Закрывает
    # blind-spot на backup CronJob'ах и failed alembic-миграциях.
    "kg-jobs-sync": {
        "task": "kg_jobs_sync",
        "schedule": crontab(minute="*/15"),
    },
    # KG Coverage #2: PVC/PV/storage signals → kg_storage_volumes + uses_volume/
    # bound_to edges. Каждые 30 мин: storage редко меняется (claim ~раз в неделю,
    # capacity статична), но мы хотим ловить phase-переходы Bound→Released в
    # течение получаса. disk_pct enrichment под флагом STORAGE_METRICS_ENABLED
    # (default OFF — kubelet_volume_stats_* может быть не настроен).
    "kg-storage-sync": {
        "task": "kg_storage_sync",
        "schedule": crontab(minute="*/30"),
    },
    # Wave 7-Z: парсер NATS subjects из исходников WO monorepo. Раз в 6h
    # делает git fetch shallow clone + grep `.cs` файлы на consumers/publish
    # call-site → upsert subject-узлы + edges kind=`uses_nats`. Идемпотентен.
    # Off by default (NATS_SUBJECTS_PARSER_ENABLED=false): требует ssh-доступ
    # к wo-gitlab и каталога `WO_MONOREPO_PATH` — включается осознанно.
    "kg-nats-subjects-sync": {
        "task": "kg_nats_subjects_sync",
        "schedule": crontab(minute=43, hour="*/6"),  # 6h, offset от drift/ingress/stuck
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


@celery_app.task(name="kg_ingress_sync")
def kg_ingress_sync_task():
    """Phase 3-B: sync k8s Ingress → external entrypoint edges."""
    from app.knowledge_graph.k8s_ingress_sync import sync_all_ingresses

    db = SessionLocal()
    try:
        return sync_all_ingresses(db)
    except Exception as e:
        logger.warning("kg_ingress_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_topology_resources_sync")
def kg_topology_resources_sync_task():
    """Wave 7 / G1.3: declarative k8s Service + Ingress resources → KG.

    Создаёт edges `serves_traffic` (Service → backing Deployment по selector)
    и `routes_to` (Ingress → backend Service). Не raise — failure внутри
    одного tick'а не должна валить beat-worker.
    """
    from app.knowledge_graph.k8s_topology_resources_sync import \
        sync_topology_resources

    db = SessionLocal()
    try:
        result = sync_topology_resources(db)
        logger.info("kg_topology_resources_sync.done result=%s", result)
        return result
    except Exception as e:
        logger.warning("kg_topology_resources_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_jobs_sync")
def kg_jobs_sync_task():
    """KG Coverage #1: k8s Job + CronJob → kg_k8s_jobs (per 15 мин).

    Не raise — failure внутри tick'а не должна валить beat-loop. См.
    `app.knowledge_graph.k8s_jobs_sync` для деталей и owner-label
    атрибуции.
    """
    from app.knowledge_graph.k8s_jobs_sync import sync_k8s_jobs

    db = SessionLocal()
    try:
        result = sync_k8s_jobs(db)
        logger.info("kg_jobs_sync.done result=%s", result)
        return result
    except Exception as e:
        logger.warning("kg_jobs_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_storage_sync")
def kg_storage_sync_task():
    """KG Coverage #2: sync PVC + PV + uses_volume/bound_to edges.

    Включает опциональный disk_pct enrichment если STORAGE_METRICS_ENABLED.
    Не raise — failure внутри одного tick'а не должна валить beat-worker.
    """
    from app.knowledge_graph.k8s_storage_sync import sync_storage

    db = SessionLocal()
    try:
        result = sync_storage(db)
        logger.info("kg_storage_sync.done result=%s", result)
        return result
    except Exception as e:
        logger.warning("kg_storage_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_external_probe")
def kg_external_probe_task():
    """External probe: DNS+TCP+HTTPS на synthetic `ingress:<host>` узлы.

    Идемпотентен: state (consecutive_failures / firing) в metadata_json.
    Безопасен к беатам-пропускам (один пропуск ≠ false-resolve).
    Шлёт Discord на FAIL_THRESHOLD-й тик подряд, resolve — при возврате в up.
    """
    import asyncio as _aio
    from app.knowledge_graph.external_probe_sync import run_external_probe

    db = SessionLocal()
    try:
        return _aio.run(run_external_probe(db))
    except Exception as e:
        logger.warning("kg_external_probe.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_health_recompute")
def kg_health_recompute_task():
    """ChatGPT review #4.3: composite health score per real service."""
    from app.knowledge_graph.health_score import recompute_all_health

    db = SessionLocal()
    try:
        return recompute_all_health(db)
    except Exception as e:
        logger.warning("kg_health_recompute.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_alerts_resolve_sync")
def kg_alerts_resolve_sync_task():
    """Точка роста #2: AlertEvent.resolved_at refresh из AM API."""
    import asyncio as _aio
    from app.knowledge_graph.alerts_resolve_sync import run_alerts_resolve_sync

    db = SessionLocal()
    try:
        return _aio.run(run_alerts_resolve_sync(db))
    except Exception as e:
        logger.warning("kg_alerts_resolve_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


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


@celery_app.task(name="kg_metrics_sync")
def kg_metrics_sync_task():
    """Per-service метрики из VM → kg_service_health (snapshot per ~10 мин)."""
    from app.knowledge_graph.metrics_sync import sync_service_health

    db = SessionLocal()
    try:
        return sync_service_health(db)
    except Exception as e:
        logger.warning("kg_metrics_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_cluster_health_sync")
def kg_cluster_health_sync_task():
    """Global cluster snapshot из VM → kg_cluster_observations (per ~5 мин)."""
    from app.knowledge_graph.cluster_health_sync import sync_cluster_health

    db = SessionLocal()
    try:
        return sync_cluster_health(db)
    except Exception as e:
        logger.warning("kg_cluster_health_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_ingress_observations_sync")
def kg_ingress_observations_sync_task():
    """Per ingress endpoint metrics → kg_ingress_observations (per ~10 мин)."""
    from app.knowledge_graph.ingress_observations_sync import \
        sync_ingress_observations

    db = SessionLocal()
    try:
        return sync_ingress_observations(db)
    except Exception as e:
        logger.warning("kg_ingress_observations_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_signal_aggregates_compute")
def kg_signal_aggregates_compute_task():
    """Pre-compute per-service агрегатов сигналов из KG (24h окно, hourly)."""
    from app.knowledge_graph.signal_aggregates import compute_signal_aggregates

    db = SessionLocal()
    try:
        return compute_signal_aggregates(db, window_hours=24)
    except Exception as e:
        logger.warning("kg_signal_aggregates_compute.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_anomaly_detection_task")
def kg_anomaly_detection_task():
    """Rolling z-score аномалии по kg_service_health → kg_anomaly_observations.

    |z|>3 на любой из 5 метрик пишет запись (severity warning/critical).
    Discord notify — отдельная фаза, эта задача только пишет notified=false.
    """
    from app.knowledge_graph.anomaly_detection import detect_anomalies

    db = SessionLocal()
    try:
        return detect_anomalies(db)
    except Exception as e:
        logger.warning("kg_anomaly_detection_task.failed: %s", e)


@celery_app.task(name="kg_runtime_correlation_sync")
def kg_runtime_correlation_sync_task():
    """Подтверждение existing edges через co-occurrence warning-событий.

    Для каждой edge ищем pairs (src.event, dst.event) с |Δt| ≤ window_minutes
    за lookback_days. Если набралось min_correlation_count+ за окно — добавляем
    "runtime_correlation" в discovery_sources (high-priority в C3 formula).

    Идемпотентно через discovery_sources merge — повторный run не дублирует.
    """
    if not settings.RUNTIME_CORRELATION_ENABLED:
        logger.info("kg_runtime_correlation_sync.skipped: disabled by config")
        return {"skipped": "disabled"}

    import asyncio
    from app.knowledge_graph.runtime_correlation import run_runtime_correlation_sync

    db = SessionLocal()
    try:
        return asyncio.run(
            run_runtime_correlation_sync(
                db,
                window_minutes=settings.RUNTIME_CORRELATION_WINDOW_MINUTES,
                min_correlation_count=settings.RUNTIME_CORRELATION_MIN_COUNT,
                lookback_days=settings.RUNTIME_CORRELATION_LOOKBACK_DAYS,
            )
        )
    except Exception as e:
        logger.warning("kg_runtime_correlation_sync.failed: %s", e)
        return {"error": str(e)}
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


@celery_app.task(name="team_daily_digest")
def team_daily_digest_task():
    """Daily per-team Discord digest. Pure data-aggregation, БЕЗ LLM.

    Итерирует все distinct team_owner из kg_services и шлёт embed per team.
    Управляется флагом settings.TEAM_DIGEST_ENABLED — если False, выходит
    сразу. См. app/services/team_digest.py.
    """
    from app.services.team_digest import send_all_team_digests

    try:
        result = asyncio.run(
            send_all_team_digests(window_hours=settings.TEAM_DIGEST_WINDOW_HOURS)
        )
        logger.info("team_daily_digest.done result=%s", result)
        return result
    except Exception as e:
        logger.error("team_daily_digest.failed: %s", e)
        # Не падаем в Celery: digest аналитический, retry смысла не имеет.
        return {"status": "error", "error": str(e)}


@celery_app.task(name="kg_self_health_check")
def kg_self_health_check_task():
    """KG self-health canary (Wave 5 retrospective).

    Runs `run_self_health_checks()` каждые 30 мин, агрегирует статус,
    пишет в audit-log один из:
        KG_SELF_HEALTH_OK     — все ok
        KG_SELF_HEALTH_WARN   — есть warn'ы, нет fail'ов
        KG_SELF_HEALTH_FAIL   — хотя бы один fail

    На FAIL — отправляет single embed в DISCORD_WEBHOOK_SELF_HEALTH_URL
    (если задан). Намеренно НЕ в #infra-error — этот сигнал для команды
    разработки copilot, не для on-call SRE.

    Idempotency: 6h dedup-окно по grubby fingerprint (sorted check_names с
    fail-статусом). In-memory state — переживает только worker-процесс,
    что и нужно: если worker рестартовал, лучше один лишний embed, чем
    пропустить регрессию.
    """
    if not settings.KG_SELF_HEALTH_ENABLED:
        return {"status": "disabled"}

    return asyncio.run(_kg_self_health_logic())


# In-memory dedup state — переживает только worker-процесс. Worker max
# tasks per child перезапускается часто, так что fingerprint живёт максимум
# ~часы. Это сознательный trade-off (см. docstring task'а).
_SELF_HEALTH_LAST_FIRE: dict[str, float] = {}
_SELF_HEALTH_DEDUP_SECONDS = 6 * 3600


async def _kg_self_health_logic() -> dict:
    import time

    from app.knowledge_graph.self_health import (aggregate_status,
                                                 fingerprint,
                                                 run_self_health_checks)
    from app.services.audit_logger import audit_service

    db = SessionLocal()
    try:
        results = run_self_health_checks(db)
    finally:
        db.close()

    overall = aggregate_status(results)
    payload = {
        "overall": overall,
        "results": [r.as_dict() for r in results],
    }
    if overall == "ok":
        audit_service.log_event("KG_SELF_HEALTH_OK", payload)
        return {"status": "ok", "checks": len(results)}
    if overall == "warn":
        audit_service.log_event("KG_SELF_HEALTH_WARN", payload)
        return {"status": "warn", "checks": len(results)}

    # overall == "fail"
    audit_service.log_event("KG_SELF_HEALTH_FAIL", payload)

    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    fp = fingerprint(results)
    now = time.time()
    last = _SELF_HEALTH_LAST_FIRE.get(fp, 0.0)
    if now - last < _SELF_HEALTH_DEDUP_SECONDS:
        logger.info(
            "kg_self_health.discord_deduped fp=%s age=%.0fs",
            fp, now - last,
        )
        return {
            "status": "fail",
            "checks": len(results),
            "failed": len(failed),
            "discord": "deduped",
        }

    try:
        from app.services.discord_service import DiscordService
        discord = DiscordService()
        await discord.send_self_health_alert(
            failed_checks=[r.as_dict() for r in failed],
            warn_checks=[r.as_dict() for r in warned],
        )
        _SELF_HEALTH_LAST_FIRE[fp] = now
        sent = True
    except Exception as e:
        logger.warning("kg_self_health.discord_failed: %s", e)
        sent = False

    return {
        "status": "fail",
        "checks": len(results),
        "failed": len(failed),
        "discord": "sent" if sent else "failed",
    }


@celery_app.task(name="kg_stuck_alerts_check")
def kg_stuck_alerts_check_task():
    """Stuck-alerts escalation: firing >MIN_DURATION_HOURS без resolved_at.

    Origin: KG-side TTR analytics (2026-05-23) показала median 29h /
    p90 83h для KubeDeploymentReplicasMismatch — реально сломанное
    состояние, похороненное под потоком свежих firing-алёртов.

    Hourly schedule, в отличие от self-health/15-мин. Длинная firing-window
    не меняется быстро, плюс audit-log на каждый tick дороже чем сигнал
    обещает дать. Idempotency: 6h dedup window по set fingerprint
    stuck-alert-id-ов (in-memory state, переживает только worker-процесс).
    """
    return asyncio.run(_kg_stuck_alerts_logic())


# In-memory dedup state — переживает только worker-процесс (как и self-health).
_STUCK_ALERTS_LAST_FIRE: dict[str, float] = {}


async def _kg_stuck_alerts_logic() -> dict:
    import time

    from app.knowledge_graph.stuck_alerts import (find_stuck_alerts,
                                                   fingerprint, group_by_team)
    from app.services.audit_logger import audit_service

    min_hours = settings.STUCK_ALERTS_MIN_DURATION_HOURS

    db = SessionLocal()
    try:
        stuck = find_stuck_alerts(db, min_duration_hours=min_hours)
    finally:
        db.close()

    if not stuck:
        audit_service.log_event("STUCK_ALERTS_NONE", {
            "min_duration_hours": min_hours,
        })
        return {"status": "ok", "stuck_count": 0}

    groups = group_by_team(stuck)

    # Per-team audit-log с count + alertnames. Один event на команду:
    # дешевле читать в дашборде чем event-per-alert.
    for g in groups:
        audit_service.log_event("STUCK_ALERTS_FOUND", {
            "team_owner": g.team_owner,
            "count": g.count,
            "alertnames": sorted({a.alertname for a in g.alerts}),
            "min_duration_hours": min_hours,
        })

    if not settings.STUCK_ALERTS_DISCORD_ENABLED:
        return {
            "status": "found",
            "stuck_count": len(stuck),
            "teams": len(groups),
            "discord": "disabled",
        }

    fp = fingerprint(stuck)
    now = time.time()
    dedup_seconds = settings.STUCK_ALERTS_DEDUP_WINDOW_HOURS * 3600
    last = _STUCK_ALERTS_LAST_FIRE.get(fp, 0.0)
    if now - last < dedup_seconds:
        logger.info(
            "kg_stuck_alerts.discord_deduped fp=%s age=%.0fs",
            fp, now - last,
        )
        return {
            "status": "found",
            "stuck_count": len(stuck),
            "teams": len(groups),
            "discord": "deduped",
        }

    try:
        from app.services.discord_service import DiscordService
        discord = DiscordService()
        await discord.send_stuck_alerts_escalation(
            team_groups=[g.as_dict() for g in groups],
            total_count=len(stuck),
            min_duration_hours=min_hours,
        )
        _STUCK_ALERTS_LAST_FIRE[fp] = now
        sent = True
    except Exception as e:
        logger.warning("kg_stuck_alerts.discord_failed: %s", e)
        sent = False

    return {
        "status": "found",
        "stuck_count": len(stuck),
        "teams": len(groups),
        "discord": "sent" if sent else "failed",
    }


@celery_app.task(name="kg_seq_logs_sync")
def kg_seq_logs_sync_task():
    """Error/Fatal логи из Seq → kg_log_observations (per ~10 мин).

    Тянет события из всех настроенных Seq-инстансов (см. SEQ_INSTANCES /
    SEQ_URL_<ENV>), агрегирует per service per level и upsert'ит в
    `kg_log_observations` через ON CONFLICT DO UPDATE. Если SEQ_*
    не сконфигурирован — task no-op.
    """
    from app.knowledge_graph.seq_logs_sync import sync_seq_logs

    db = SessionLocal()
    try:
        return sync_seq_logs(db, window_minutes=10)
    except Exception as e:
        logger.warning("kg_seq_logs_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_nats_subjects_sync")
def kg_nats_subjects_sync_task():
    """Wave 7-Z: парсер NATS subjects из исходников WO monorepo.

    Раз в 6h:
      1. git clone/fetch shallow + sparse (`GR.Platform`, `GR.WO.*`)
      2. grep `.cs` на consumer-overrides + `SendToJetStreamAsync(...)`
      3. upsert synthetic subject-узлы `subject:<x>` в namespace
         `nats-subjects` + edges `uses_nats` с `extras.direction = pub|sub`

    Off by default через `NATS_SUBJECTS_PARSER_ENABLED=false` — требует
    ssh-доступа к wo-gitlab и каталога `WO_MONOREPO_PATH`. Включается
    осознанно после ручного дры-ран на тестовой инсталляции.
    """
    if not getattr(settings, "NATS_SUBJECTS_PARSER_ENABLED", False):
        logger.info("kg_nats_subjects_sync.skipped reason=disabled")
        return {"skipped": True, "reason": "NATS_SUBJECTS_PARSER_ENABLED=false"}

    from app.knowledge_graph.nats_subjects_sync import sync_nats_subjects

    db = SessionLocal()
    try:
        return sync_nats_subjects(db)
    except Exception as e:
        logger.warning("kg_nats_subjects_sync.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()
