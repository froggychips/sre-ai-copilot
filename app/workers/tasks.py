import asyncio
import logging

import httpx
from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.core.state_machine import IncidentState
from app.database import IncidentRecord, ReadOnlyAutocommitSession, SessionLocal
from app.services.audit_logger import audit_service
from app.services.telemetry_utils import incident_span
from app.telemetry import setup_telemetry
from app.workers.pipeline import IncidentPipeline, transition_to
from app.workers.task_lock import single_instance

setup_telemetry(service_name="copilot-worker")

logger = logging.getLogger(__name__)

celery_app = Celery("sre_tasks", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

# task_track_started: выставляем безусловно (в т.ч. eager) — раньше это жило
# на legacy-app Celery("worker") в celery_worker.py, который теперь сведён сюда.
# /copilot-флоу (generate_reply) опрашивает AsyncResult в main.py и опирается
# на STARTED-состояние для in-flight задач; без него running-задача рапортует
# PENDING. Не зависит от backpressure-блока ниже (тот gated на non-eager).
celery_app.conf.task_track_started = True

# ── Late acknowledgement (задача переживает смерть воркера) ─────────────────
#
# ПРОБЛЕМА, которую это чинит. По умолчанию Celery работает в acks_early:
# брокер вычёркивает сообщение из очереди в момент ВЫДАЧИ воркеру, ещё до
# первой строки таска. Если воркер умирает в середине обработки — OOMKill
# (см. арифметику лимитов в k8s/worker.yaml), eviction по imagefs, rollout,
# drain ноды — задача уже заacked. Она не будет переотправлена: инцидент
# просто не обработан, ретрая не будет, следа в очереди не останется.
# Для системы, чья работа — «не проспать инцидент», это худший класс отказа:
# тихая потеря без единого сигнала.
#
# `task_acks_late=True` переносит ack на момент ПОСЛЕ выполнения, поэтому
# смерть воркера возвращает сообщение в очередь, и его подхватывает живая
# реплика (их 2, см. replicas в k8s/worker.yaml и helm values).
#
# `task_reject_on_worker_lost=True` — без него потеря воркера отдаёт задаче
# WorkerLostError и она помечается FAILURE (то есть всё равно теряется,
# просто с трейсом). True = сообщение реджектится с requeue.
#
# `task_acks_on_failure_or_timeout=True` (дефолт, но фиксируем явно, потому
# что это единственная защита от poison-message-петли): обычный exception и
# срабатывание soft/hard time limit'а задачу ПОДТВЕРЖДАЮТ, а не возвращают
# в очередь. Иначе таск, который стабильно падает или стабильно упирается в
# 30-минутный hard limit, крутился бы по кругу вечно, занимая слот. Ретраями
# управляет `autoretry_for` (см. RETRIABLE_EXC ниже), а не брокер.
#
# ВЗАИМОДЕЙСТВИЕ С УЖЕ НАСТРОЕННЫМ BACKPRESSURE (смысл сохранён):
#   * `worker_prefetch_multiplier=1` — штатная пара к acks_late: воркер держит
#     ровно одно неподтверждённое сообщение на слот. Без этого при acks_late
#     под воркером копилась бы пачка зарезервированных, но неподтверждённых
#     задач, и его смерть возвращала бы их все разом.
#   * `worker_max_tasks_per_child=50` — плановый recycle ребёнка происходит
#     ПОСЛЕ завершения и ack'а задачи, это штатный выход, а не worker-lost;
#     ложных переотправок не даёт.
#   * time limit'ы не тронуты: 1500s soft / 1800s hard.
#
# КРИТИЧНО ДЛЯ REDIS-БРОКЕРА: у redis-транспорта нет настоящего ack —
# «неподтверждённое» сообщение возвращается в очередь по `visibility_timeout`.
# Если он окажется МЕНЬШЕ времени выполнения, redis отдаст сообщение второму
# воркеру, пока первый ещё работает: два параллельных прогона одного инцидента.
# Поэтому держим окно заведомо больше hard time limit'а (×2). Сейчас это
# 3600s при hard limit 1800s — то же, что дефолт kombu, но теперь связь
# зафиксирована: поднимут CELERY_TASK_TIME_LIMIT_SECONDS — окно поедет само.
#
# ИДЕМПОТЕНТНОСТЬ (почему повторный прогон безопасен). При acks_late задача
# может выполниться ПОВТОРНО — воркер мог умереть уже после side-effect'а, но
# до ack'а. Что защищает:
#   * `process_incident` — checkpoint/resume в pipeline.py (`_CHECKPOINT_KEY`
#     в record.analysis + `_COMPLETED_BY_STATE`): уже закоммиченные стадии
#     пропускаются, их вывод берётся из checkpoint-а, повторного прожига LLM
#     нет. `transition_to` толерантен к re-entry (`_PIPELINE_STATE_ORDER`) —
#     повтор стадии не падает на «Invalid transition».
#   * Реальный kubectl-write в этом таске НЕ живёт: apply идёт отдельным
#     путём (app/services/executor_apply.py, кнопка на Discord-embed) и закрыт
#     row-lock'ом + маркером `analysis.executor_applied` — повторный claim
#     получает отказ already_applied, двойного apply быть не может.
#   * kg_*-синки — upsert'ы по естественным ключам (event_uid, (service_id,
#     ts), (service_id, buildtype_id, build_number), ON CONFLICT DO UPDATE):
#     повторный прогон переписывает те же строки.
#   * Discord — cross-replica PATCH-dedup в таблице `discord_dedup`
#     (app/services/discord/dedup_store.py), не per-process: повторная
#     доставка не даёт второй POST в пределах TTL-окна.
# Тасков с незащищённым необратимым внешним write'ом здесь нет. Известный
# остаток — дайджесты (daily_stats_digest / team_daily_digest /
# chronic_alerts_digest) и in-memory dedup у kg_self_health_check /
# kg_stuck_alerts_check: повторная доставка может продублировать сообщение в
# канал. Это шум, а не повреждение данных; терять дайджест молча хуже.
# Если какой-то таск всё же понадобится вернуть в acks_early — это делается
# точечно: `@celery_app.task(..., acks_late=False)`, глобальную настройку
# ломать не нужно.
#
# Аварийный выключатель читается через getattr: чтобы `CELERY_ACKS_LATE=false`
# в env реально работал, поле нужно объявить в app/config.py (Settings стоит
# на extra="ignore" — необъявленный env-var молча игнорируется). Пока поля
# нет, значение всегда True.
_ACKS_LATE: bool = bool(getattr(settings, "CELERY_ACKS_LATE", True))

# Backpressure-настройки (см. config.py для пояснений).
# Не применяются в eager-mode чтобы тесты не упирались в time-limit'ы.
if not settings.CELERY_TASK_ALWAYS_EAGER:
    celery_app.conf.update(
        worker_prefetch_multiplier=settings.CELERY_WORKER_PREFETCH_MULTIPLIER,
        worker_max_tasks_per_child=settings.CELERY_WORKER_MAX_TASKS_PER_CHILD,
        # Recycle по ПАМЯТИ, а не только по счётчику задач. До 16.08.2026
        # этой настройки не было вовсе, и форк жил до 50 задач независимо от
        # того, во что вырос: замер показал базу 306/242/213/175 МБ у четырёх
        # форков в покое, при том что каждая тяжёлая задача добавляет сверху
        # ещё 200-250 МБ. Итог — 14 OOMKill за двое суток при лимите 3Gi.
        #
        # Ни одна задача сама по себе не тяжёлая (максимум 244 МБ пика);
        # проблема в том, что Python не возвращает память ОС, и база
        # долгоживущего форка только растёт.
        worker_max_memory_per_child=settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB,
        task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,
        task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS,
        # Celery 6 deprecation — без этого warning на старте worker'а.
        broker_connection_retry_on_startup=True,
        # См. блок «Late acknowledgement» выше.
        task_acks_late=_ACKS_LATE,
        task_reject_on_worker_lost=_ACKS_LATE,
        task_acks_on_failure_or_timeout=True,
        broker_transport_options={
            "visibility_timeout": max(
                3600, settings.CELERY_TASK_TIME_LIMIT_SECONDS * 2
            ),
        },
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
        "options": {"expires": 3240},
    },
    # Daily stats digest (включается через STATS_DIGEST_ENABLED).
    # БЕЗ LLM-вызовов — pure aggregation, шлёт в DISCORD_WEBHOOK_STATS_URL.
    "daily-stats-digest": {
        "task": "daily_stats_digest",
        "schedule": crontab(hour=settings.STATS_DIGEST_HOUR_UTC, minute=0),
        "options": {"expires": 21600},
    },
    # TC deploys → kg_deployments. БЕЗ LLM. Включает RecentDeployRule
    # на live deploys без incident-flow. Каждые 15 минут берёт recent
    # builds из TC за 24h и upsert-ит в kg_deployments по (service, sha).
    "tc-deploys-to-kg": {
        "task": "tc_deploys_to_kg",
        "schedule": crontab(minute="14,29,44,59"),
        "options": {"expires": 810},
    },
    # L5 (alert-fatigue): chronic-alerts digest в канал #stats каждые 6h.
    # Гасит mute-эффект от suppress-chronic — даёт видимость сервисов
    # которые тлеют под suppress. Управляется CHRONIC_DIGEST_ENABLED.
    "chronic-alerts-digest": {
        "task": "chronic_alerts_digest",
        "schedule": crontab(minute=33, hour="*/6"),
        "options": {"expires": 10800},
    },
    # A4: k8s pod-events → kg_pod_events. Каждые 10 мин тянем Warning-events
    # из всех ns с deployments в KG. Idempotent по event_uid. Источник
    # OOMKilled / FailedScheduling / ImagePullBackOff / Unhealthy / etc.
    "k8s-pod-events-sync": {
        "task": "k8s_pod_events_sync",
        "schedule": crontab(minute="1,11,21,31,41,51"),
        "options": {"expires": 540},
    },
    # D2-auto: drift cleanup. Раз в час пересинхронизирует kg_services со
    # списком существующих в k8s ns. Safety threshold 20% — защита от
    # mass-mark при временной недоступности API server.
    # Ретеншен истории метрик. Ночью и раз в сутки: спешить некуда, а
    # удаление батчами всё равно даёт работу автовакууму.
    # Endpoints: каждые 15 минут, НЕ в такт остальным синкам.
    "kg-endpoints-sync": {
        "task": "kg_endpoints_sync",
        # Смещение с ровного часа НАМЕРЕННОЕ. Замер 16.08.2026: worker
        # держит `--concurrency=4`, каждый форк после тяжёлого синка сохраняет
        # 200-250 МБ, и когда несколько таких задач идут разом, сумма упирается
        # в лимит 3Gi — 11 OOMKill за 19 часов. Пики сходились на `:00`, где
        # встречались topology (0 * * * *), endpoints (*/15) и
        # namespace-lifecycle (*/10).
        #
        # Ни один синк по отдельности не тяжёлый: endpoints 122 МБ пик,
        # k8s_topology_resources 244 МБ, kg_topology_sync 229 МБ. Проблема
        # ровно в совпадении, поэтому лечится расписанием, а не оптимизацией.
        "schedule": crontab(minute="7,22,37,52"),
        "options": {"expires": 600},
    },
    "kg-health-retention": {
        "task": "kg_health_retention",
        "schedule": crontab(hour=3, minute=40),
        "options": {"expires": 3600},
    },
    "kg-namespace-lifecycle": {
        "task": "kg_namespace_lifecycle",
        # Каждые 10 минут: пересоздание сквада занимает минуты, и час — слишком
        # грубое окно, чтобы отличить «пересоздали» от «был недоступен».
        # Смещено на :03 — см. комментарий у kg-endpoints-sync про совпадение
        # пиков памяти на ровном часе.
        "schedule": crontab(minute="3,13,23,33,43,53"),
        "options": {"expires": 540},
    },
    "kg-db-edge-rehome": {
        "task": "kg_db_edge_rehome",
        # Раз в час и на :07 — заведомо ПОСЛЕ kg_namespace_lifecycle
        # (:3,13,23,...): отбор опирается на состояние namespace, и считать
        # его надо по свежей таблице. Чаще незачем — источник пополняется
        # только при пересоздании окружений.
        "schedule": crontab(minute="7"),
        # Меньше часового интервала: иначе в очереди могли бы жить два тика
        # одной задачи, а она пишет в граф.
        "options": {"expires": 1800},
    },
    "kg-drift-cleanup": {
        "task": "kg_drift_cleanup",
        "schedule": crontab(minute=17),  # ежечасно в 17 мин
        "options": {"expires": 3240},
    },
    # Точка роста #2 (Phase 2): AlertEvent resolve sync. Каждые 15 мин
    # сравниваем kg_alerts.fingerprint с активными на AM, не-firing
    # помечаем resolved_at=NOW. Без этого stale firing alerts копятся
    # годами (см. etcdMembersDown от 10 апреля).
    "kg-alerts-resolve-sync": {
        "task": "kg_alerts_resolve_sync",
        "schedule": crontab(minute="5,20,35,50"),
        "options": {"expires": 810},
    },
    # Phase 3-B: k8s Ingress → external entrypoint edges. Раз в час
    # синхронизирует Ingress resources cluster-wide. Создаёт synthetic-узлы
    # `ingress:<host>` + edges на backend services. Это первый источник
    # «откуда приходит трафик» вне cluster-internal env-scan.
    "kg-ingress-sync": {
        "task": "kg_ingress_sync",
        "schedule": crontab(minute=37),  # ежечасно в 37 мин (offset от других)
        "options": {"expires": 3240},
    },
    # ChatGPT review #4.3: service health composite (open alerts × severity
    # + chronic pod events + recurrence). Раз в 20 мин — health моментальный
    # сигнал, но recompute дорого над всеми ~370 real services. Используется
    # в kg_fragile_top для «истинного» ранжирования и в digest «🩺 Unhealthy».
    "kg-health-recompute": {
        "task": "kg_health_recompute",
        "schedule": crontab(minute="15,35,55"),
        "options": {"expires": 1080},
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
        "options": {"expires": 50},
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
        "schedule": crontab(minute="8,18,28,38,48,58"),
        # expires < интервал: если воркер не подхватил тик за 9 мин (backlog),
        # дропаем его вместо накопления параллельных прогонов — наложение
        # перегружало одиночный vmsingle (recon 2026-06-05, см. metrics_sync).
        "options": {"expires": 540},
    },
    # Global cluster snapshot — те же поля что в #stats daily report,
    # но раз в 5 мин и материализованно. Используется для trend-аналитики
    # и post-mortem контекста.
    "kg-cluster-health-sync": {
        "task": "kg_cluster_health_sync",
        "schedule": crontab(minute="*/5"),
        "options": {"expires": 270},
    },
    # Per ingress endpoint observations (p95/p99/rps/4xx/5xx) из nginx-ingress
    # exporter. host/path берутся из k8s Ingress resources (kubectl get -A).
    "kg-ingress-observations-sync": {
        "task": "kg_ingress_observations_sync",
        "schedule": crontab(minute="6,16,26,36,46,56"),
        "options": {"expires": 540},
    },
    # Pre-compute per-service агрегаты сигналов из САМОГО KG (deploys/alerts/
    # pod_events) за 24h окно. Hourly. Не ходит в VM — pure SQL.
    "kg-signal-aggregates-compute": {
        "task": "kg_signal_aggregates_compute",
        "schedule": crontab(minute=23),  # ежечасно в 23 мин (offset от drift/ingress)
        "options": {"expires": 3240},
    },
    # Anomaly detection (2026-05-22, миграция 20260522_0200):
    # rolling z-score по kg_service_health для каждой из 5 метрик.
    # При |z|>3 пишем в kg_anomaly_observations (severity warning/critical).
    # Идемпотентно по (service_id, ts, metric). Discord-уведомление — фаза 2.
    "kg-anomaly-detection-task": {
        "task": "kg_anomaly_detection_task",
        "schedule": crontab(minute="4,14,24,34,44,54"),
        "options": {"expires": 540},
    },
    # Runtime correlation: подтверждает existing edges через co-occurrence
    # warning-событий (BackOff/Unhealthy/OOMKilled/...) у src+dst в окне 15 мин.
    # Sliding window 7 дней — дорогой запрос, /30 мин достаточно.
    # Управляется RUNTIME_CORRELATION_ENABLED.
    "kg-runtime-correlation-sync": {
        "task": "kg_runtime_correlation_sync",
        "schedule": crontab(minute="18,48"),
        "options": {"expires": 1620},
    },
    # Per-team daily digest — один embed per team_owner (squad-N / infra /
    # monitoring). Зависит от kg_signal_aggregates (slo_burn_pct) и
    # kg_services.health_score — должен запускаться ПОСЛЕ их compute.
    # Управляется TEAM_DIGEST_ENABLED.
    "team-daily-digest": {
        "task": "team_daily_digest",
        "schedule": crontab(hour=settings.TEAM_DIGEST_HOUR_UTC, minute=0),
        "options": {"expires": 21600},
    },
    # Error/Fatal логи из Seq → kg_log_observations. Тянет по настроенным
    # Seq-инстансам (prod/preprod/preupdate) count событий за окно ~10 мин,
    # агрегирует per service per level и пишет одну строку через
    # ON CONFLICT DO UPDATE. Если SEQ_* пусто — no-op.
    "kg-seq-logs-sync-task": {
        "task": "kg_seq_logs_sync",
        "schedule": crontab(minute="9,19,29,39,49,59"),
        "options": {"expires": 540},
    },
    # KG self-health canary (Wave 5 retrospective): «monitoring of the
    # monitoring». Ищет тихие деградации в наших же KG-таблицах
    # (mem_pct=0 за неделю, sync_lag, stale alerts, и т.п.). На FAIL —
    # audit-log + опциональный Discord embed в DISCORD_WEBHOOK_SELF_HEALTH_URL
    # (отдельный dev-канал, не #infra-error). Idempotency: 6h dedup window.
    "kg-self-health-check": {
        "task": "kg_self_health_check",
        "schedule": crontab(minute="21,51"),
        "options": {"expires": 1620},
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
        "options": {"expires": 3240},
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
        "schedule": crontab(minute="12,27,42,57"),
        "options": {"expires": 810},
    },
    # Второй, независимый от TeamCity источник деплоев: сам кластер.
    # Сравнивает generation/образы workload'ов с прошлым прогоном и пишет
    # выкат туда, где его прочтёт RecentDeployRule. Замер 05.09.2026: из
    # 137 423 записей kg_deployments точную цель имели 36 — всё остальное
    # приходило ns-broadcast'ом от TeamCity. Пять минут — компромисс между
    # «успеть до алерта» (RecentDeployRule смотрит на 60 минут назад) и
    # весом `kubectl get deploy,sts,ds -A -o json`.
    "kg-deploy-watch": {
        "task": "kg_deploy_watch",
        "schedule": crontab(minute="3,8,13,18,23,28,33,38,43,48,53,58"),
        "options": {"expires": 270},
    },
    # KG Coverage #1: k8s Job + CronJob → kg_k8s_jobs. Каждые 15 мин
    # `kubectl get jobs,cronjobs -A` + upsert. Сигналы: last_successful_time
    # / last_schedule_time / failed_count / last_pod_exit_code. Закрывает
    # blind-spot на backup CronJob'ах и failed alembic-миграциях.
    "kg-jobs-sync": {
        "task": "kg_jobs_sync",
        "schedule": crontab(minute="10,25,40,55"),
        "options": {"expires": 810},
    },
    # KG Coverage #2: PVC/PV/storage signals → kg_storage_volumes + uses_volume/
    # bound_to edges. Каждые 30 мин: storage редко меняется (claim ~раз в неделю,
    # capacity статична), но мы хотим ловить phase-переходы Bound→Released в
    # течение получаса. disk_pct enrichment под флагом STORAGE_METRICS_ENABLED
    # (default OFF — kubelet_volume_stats_* может быть не настроен).
    "kg-storage-sync": {
        "task": "kg_storage_sync",
        "schedule": crontab(minute="26,56"),
        "options": {"expires": 1620},
    },
    # Wave 7-Z: парсер NATS subjects из исходников WO monorepo. Раз в 6h
    # делает git fetch shallow clone + grep `.cs` файлы на consumers/publish
    # call-site → upsert subject-узлы + edges kind=`uses_nats`. Идемпотентен.
    # Off by default (NATS_SUBJECTS_PARSER_ENABLED=false): требует ssh-доступ
    # к wo-gitlab и каталога `WO_MONOREPO_PATH` — включается осознанно.
    "kg-nats-subjects-sync": {
        "task": "kg_nats_subjects_sync",
        "schedule": crontab(minute=43, hour="*/6"),  # 6h, offset от drift/ingress/stuck
        "options": {"expires": 3240},
    },
    # Periodic ownership backfill (2026-05-24): закрывает gap multi-signal
    # owner inference, который интегрирован только в digest. Каждые 6 часов
    # проходит по сервисам без owner и применяет high-confidence-кандидатов
    # (порог = OWNERSHIP_BACKFILL_THRESHOLD). Default OFF — включается через
    # OWNERSHIP_BACKFILL_ENABLED. Offset minute=17 чтобы не наложиться на
    # drift-cleanup (тот же 17 min, но schedule отличается).
    "kg-ownership-backfill": {
        "task": "kg_ownership_backfill",
        "schedule": crontab(minute=17, hour="*/6"),
        "options": {"expires": 3240},
    },
    # Janitor для discord_dedup: вынесенный из hot-path get_fresh purge
    # stale-строк (Infra H4). Раз в 10 мин — таблица всегда в пределах
    # активных групп алертов, отставание purge от TTL некритично (свежесть
    # всё равно сверяется по first_ts в get_fresh). Offset minute=29.
    "discord-dedup-purge": {
        "task": "discord_dedup_purge",
        "schedule": crontab(minute="29,59"),
        "options": {"expires": 3240},
    },
    # Statics version delta tracking (инцидент 2026-07-02): каждые 5 мин
    # наблюдает номер версии статики для STATICS_TRACK_ENVS и держит «до»-снимок
    # в Redis (statics:seen:<env>). Даёт enrichment'у время наката (bump), чтобы
    # волну self-restart'ов не путать с cross-ns collateral. No-op если
    # STATICS_* не настроен. Лёгкий — 4 быстрых pg_database-запроса.
    "kg-statics-versions-sync": {
        "task": "kg_statics_versions_sync",
        "schedule": crontab(minute="2,7,12,17,22,27,32,37,42,47,52,57"),
        "options": {"expires": 270},
    },
}


# Авто-ретрай ТОЛЬКО для transient-сбоев (сеть/БД/HTTP-транспорт): сетевой
# обрыв до Redis/PG, таймаут, недоступность апстрима — это recoverable, имеет
# смысл повторить с экспоненциальным backoff + jitter. НЕ ретраим generic
# Exception / ValueError / ValidationError — это non-recoverable (битый payload,
# баг в коде): повторять их 3× бессмысленно и только жжёт LLM-бюджет.
#
# Один источник правды: и Celery `autoretry_for`, и решение «писать ли FAILED»
# в async_process_incident читают ЭТОТ кортеж. isinstance(exc, RETRIABLE_EXC) —
# ровно тот же тест, которым Celery решает «ретраить ли», поэтому terminal-
# проверка (см. async_process_incident) не может разъехаться с реальным
# retry-поведением.
RETRIABLE_EXC = (
    ConnectionError,
    OSError,
    OperationalError,
    httpx.TransportError,
    httpx.TimeoutException,
)


@celery_app.task(
    name="process_incident",
    bind=True,
    max_retries=3,
    # NB про stage-cap: на Python 3.11+ builtin TimeoutError — сабкласс OSError,
    # т.е. голый TimeoutError попадал бы в RETRIABLE_EXC через OSError. Поэтому
    # per-stage cap в pipeline.run() (_staged) НЕ выпускает TimeoutError наружу,
    # а конвертирует его в PipelineStageTimeout (не входит в RETRIABLE_EXC) —
    # намеренный терминальный таймаут стадии не ретраится и не пережигает
    # LLM-бюджет, контракт «stage-cap → FAILED» сохраняется. Сетевые таймауты
    # (httpx.TimeoutException / OSError) остаются ретраибельными.
    autoretry_for=RETRIABLE_EXC,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    rate_limit=settings.CELERY_PROCESS_INCIDENT_RATE_LIMIT,
)
def process_incident_task(self, incident_data: dict):
    # Hard-gate проверяется внутри `async_process_incident` чтобы закрыть
    # оба entry-point (Celery task + PIPELINE_DIRECT_INVOKE из webhook).
    # asyncio.run создаёт чистый event loop на задачу — get_event_loop()
    # на Python 3.10+ выкидывает DeprecationWarning, а в Python 3.14+
    # без running loop возвращает ошибку. Celery worker — синхронный
    # context, eager-режим (тесты) тоже работает корректно с asyncio.run.
    #
    # Пробрасываем retry-контекст: FAILED (терминал) пишем только когда Celery
    # больше НЕ будет ретраить — иначе следующая попытка стартует на FAILED-
    # строке, валится на невалидном FAILED→INVESTIGATING переходе и жжёт лишний
    # Analyzer-вызов (см. terminal-логику в async_process_incident).
    return asyncio.run(
        async_process_incident(
            incident_data,
            retries=self.request.retries,
            max_retries=self.max_retries,
        )
    )


async def async_process_incident(
    incident_data: dict, retries: int = 0, max_retries: int = 0
):
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
        # record объявляем до try: except-ветка на него смотрит и не должна
        # падать на NameError, если упал сам query.
        record = None
        try:
            # Query — внутри try: OperationalError на нём раньше улетал МИМО
            # finally и оставлял сессию (коннект из пула) незакрытой.
            record = (
                db.query(IncidentRecord)
                .filter(IncidentRecord.incident_id == incident_id)
                .first()
            )
            pipeline = IncidentPipeline(incident_data, db, record, _root_span)
            await pipeline.run()
        except Exception as e:
            # FAILED — терминальное состояние. Если писать его ПЕРЕД Celery-
            # авторетраем, следующая попытка стартует уже на FAILED-строке,
            # падает на невалидном переходе FAILED→INVESTIGATING и впустую
            # жжёт Analyzer-вызов, маскируя исходную ошибку. Поэтому фиксируем
            # провал только когда он терминальный: либо exception non-retriable
            # (Celery его не ретраит — isinstance-тест тот же, что в
            # autoretry_for), либо это последняя разрешённая попытка.
            is_retriable = isinstance(e, RETRIABLE_EXC)
            is_terminal = (not is_retriable) or (retries >= max_retries)
            if not is_terminal:
                # Transient-сбой с оставшимися попытками — НЕ трогаем статус,
                # чтобы авторетрай возобновил инцидент с его текущего состояния.
                # Откатываем только незакоммиченный мусор текущей сессии.
                db.rollback()
                raise e
            if record is not None:
                # Пишем post-mortem в analysis ДО перехода в FAILED, чтобы
                # упавшая строка была self-describing (видна причина без
                # похода в OTel/логи). Отдельный try/except — persist-сбой
                # не должен маскировать исходную ошибку pipeline-а.
                try:
                    record.analysis = {  # type: ignore[assignment]
                        **(record.analysis or {}),
                        "failed": {
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    }
                    flag_modified(record, "analysis")
                    db.commit()
                except Exception as persist_err:
                    logger.warning(
                        "async_process_incident.postmortem_persist_failed: %s",
                        persist_err,
                    )
                    db.rollback()
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
@single_instance(ttl_seconds=3600)
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


@celery_app.task(name="k8s_pod_events_sync")
@single_instance(ttl_seconds=1800)
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
@single_instance(ttl_seconds=1800)
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
@single_instance(ttl_seconds=3600)
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


@celery_app.task(name="kg_deploy_watch")
@single_instance(ttl_seconds=600)
def kg_deploy_watch_task():
    """Выкаты, замеченные в кластере, а не рассказанные TeamCity.

    До 05.09.2026 `kg_deployments` наполнялся из одного источника, и 99,97%
    его записей были ns-broadcast'ом — утверждением «в namespace что-то
    каталось», разосланным всем сервисам. Точную цель имели 36 записей из
    137 423.

    Здесь источник другой: `metadata.generation` и образы контейнеров,
    которые кластер ведёт сам. Записи получают `namespace_scope=False`,
    то есть служат доказательством деплоя КОНКРЕТНОГО сервиса — на таком
    входе `stale_classifier` наконец может выдать `active`.

    Первый прогон только запоминает состояние: сравнивать не с чем.
    """
    from app.knowledge_graph.k8s_deploy_watch import watch_k8s_rollouts

    db = SessionLocal()
    try:
        result = watch_k8s_rollouts(db)
        logger.info("kg_deploy_watch.done result=%s", result)
        return result
    except Exception as e:
        logger.warning("kg_deploy_watch.failed: %s", e)
        db.rollback()
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
@single_instance(ttl_seconds=1800)
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


@celery_app.task(name="kg_namespace_lifecycle")
@single_instance(ttl_seconds=1800)
def kg_namespace_lifecycle_task():
    """B2: сверить kg_namespaces с кластером — присутствие и инкарнации.

    Дешёвый таск: один `kubectl get ns -o json` и апдейт таблицы из ~200
    строк. Отдельный от kg_topology_sync намеренно — он должен видеть ВСЕ
    namespace кластера, а не только те, что синк обходит; именно расхождение
    между этими списками и было слепой зоной (25 обходимых против 139 живых).

    Помечает присутствие и считает инкарнации, а затем убирает рёбра,
    не подтверждённые после пересоздания стенда. Второе — шаг B5, который
    раньше был отложен «до недели наблюдений»: за эту неделю стало видно,
    что рёбра прежнего воплощения оживают вместе с именем namespace. Замер
    21.08.2026: 1033 таких ребра в 22 пересозданных namespace, из них 636
    `uses_db`, 388 `routes_to`, 374 `uses_nats`.

    Узлы по-прежнему не удаляются: они переиспользуются новым воплощением
    (upsert по namespace+name), и их судьба — забота `drift_cleanup`.
    """
    from app.knowledge_graph.namespace_lifecycle import (
        purge_stale_edges_after_reincarnation,
        purge_stale_health_after_reincarnation, sync_namespace_lifecycle)

    db = SessionLocal()
    try:
        stats = sync_namespace_lifecycle(db)
        if settings.KG_REINCARNATION_PURGE_ENABLED:
            stats["reincarnation_purge"] = purge_stale_edges_after_reincarnation(
                db, apply=True,
            )
            # Health-точки прежнего воплощения важнее рёбер: по ним детектор
            # аномалий строит baseline, и после пересоздания стенда он
            # сравнивает новый с прежним. Замер 21.08.2026 — 262 657 точек
            # прежних инкарнаций внутри семидневного окна, 797 затронутых
            # сервисов, и 133 сервиса аномальны больше двадцати часов из
            # двадцати четырёх.
            stats["health_purge"] = purge_stale_health_after_reincarnation(
                db, apply=True,
            )
        return stats
    except Exception as e:
        logger.warning("kg_namespace_lifecycle.failed: %s", e)
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_db_edge_rehome")
@single_instance(ttl_seconds=1800)
def kg_db_edge_rehome_task():
    """Вернуть рёбра `uses_db` в базы своего окружения.

    Разовым скриптом это не закрывается, и вот почему. 21.08.2026 перенос
    3740 рёбер прошёл, проверка вышла в ok — а через сорок минут в графе
    снова оказалось 64 таких ребра. Разбор: рёбра созданы 08.08 и никуда не
    появлялись, изменилось СОСТОЯНИЕ источника. `squad-10-kingdom2`
    пересоздали (incarnation 3, namespace в кластере моложе получаса), он
    перешёл из missing в active, и его старые рёбра на базы удалённого
    preprod-kingdom1 снова стали ложью о работающем окружении.

    Пока сквады пересоздаются, а старые рёбра переживают смену инкарнации,
    отбор будет пополняться сам. Поэтому задача периодическая, а не
    одноразовая.

    Идемпотентна: на исправленном графе — no-op. Ничего не создаёт: если
    правильного узла в своём окружении нет, ребро остаётся на месте (так
    ведут себя, например, рёбра снесённого squad-20-shared, у которого
    db-узлов в графе нет вовсе).

    Отключается `KG_DB_EDGE_REHOME_ENABLED=false`: задача пишет в граф, и
    выключатель на такое нужен.
    """
    if not settings.KG_DB_EDGE_REHOME_ENABLED:
        return {"status": "disabled"}

    from app.knowledge_graph.db_edge_rehome import rehome_db_edges

    db = SessionLocal()
    try:
        return rehome_db_edges(db, apply=True)
    except Exception as e:
        logger.warning("kg_db_edge_rehome.failed: %s", e)
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_endpoints_sync")
@single_instance(ttl_seconds=900)
def kg_endpoints_sync_task():
    """Сверить Service-узлы с реальными endpoints кластера.

    Отвечает на вопрос, которого граф не знал: стоят ли за Service живые
    поды. Замер 15.08.2026 — 4732 Service с адресами и 83 без, причём среди
    пустых нет ни одного headless или ExternalName: все 83 аномальны.

    Каждые 15 минут, рядом с топологическим синком: endpoints меняются при
    каждом рестарте пода, и часовое окно давало бы устаревшую картину.
    """
    from app.knowledge_graph.k8s_endpoints_sync import sync_endpoints

    db = SessionLocal()
    try:
        return sync_endpoints(db)
    except Exception as e:
        logger.warning("kg_endpoints_sync.failed: %s", e)
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="kg_health_retention")
@single_instance(ttl_seconds=3600)
def kg_health_retention_task():
    """Удалить точки kg_service_health старше срока хранения.

    Политики хранения у таблицы не было вообще: к 15.08.2026 она выросла до
    18.2 млн строк и 4.2 ГБ — 79% всей базы, при том что самый глубокий
    потребитель (baseline детектора аномалий) смотрит на 7 дней.

    Раз в сутки и ночью: удаление батчами всё равно нагружает автовакуум, а
    срочности в нём никакой.
    """
    from app.knowledge_graph.health_retention import purge_old_health

    db = SessionLocal()
    try:
        return purge_old_health(db, retention_days=settings.KG_HEALTH_RETENTION_DAYS)
    except Exception as e:
        logger.warning("kg_health_retention.failed: %s", e)
        db.rollback()
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


@celery_app.task(name="discord_dedup_purge")
def discord_dedup_purge_task():
    """Janitor discord_dedup: удалить stale-строки PATCH-dedup.

    Purge вынесен из hot-path get_fresh (Infra H4) — теперь это периодический
    DELETE здесь. TTL = ENRICHED_DEDUP_WINDOW_SECONDS (то же окно, что
    использует send_enriched_alert). БЕЗ LLM-вызовов.
    """
    from app.services.discord.dedup_store import purge_stale

    try:
        deleted = purge_stale(settings.ENRICHED_DEDUP_WINDOW_SECONDS)
        return {"deleted": deleted}
    except Exception as e:
        logger.warning("discord_dedup_purge.failed: %s", e)
        return {"error": str(e)}


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
    from app.services.teamcity_service import (branch_for_namespace,
                                               is_prod_buildtype as _is_prod_buildtype,
                                               recent_deploys)

    # Берём builds за последние 24h. Cron каждые 15 мин — overkill по
    # window, но dedup защищает и cheaply tolerant к беатам-пропускам.
    builds = await recent_deploys(lookback_hours=24, limit=200)
    if not builds:
        return {"builds_fetched": 0, "kg_deployments_added": 0}

    db = SessionLocal()
    added = 0
    stats_skipped_no_realm = 0
    by_attribution: dict[str, int] = {"build_param": 0, "vcs_branch": 0}
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
            # TC рапортует ветку дефолтных deploy-конфигов литералом '<default>'
            # (BuildAndDeploy/OneServiceBuildAndUpdate), а не именем ветки. Для
            # K8sNewCluster дефолт == preprod (VCS-роуты *Preprod). Без этой
            # нормализации целый класс preprod-деплоев молча дропался (ns_match=0)
            # → deploy-stream протухал, как только переставали идти явно-preprod
            # билды (BuildAndUpdate). Prod-деплои несут ветку 'prod' явно, prod-
            # конфиги содержат 'Prod_' в id — их под '<default>' не подменяем.
            bt_id = b.get("buildtype_id") or ""
            if _is_prod_buildtype(bt_id):
                # Прод-джобы TC запускаются на ветке preprod: оттуда берётся
                # ТОЛЬКО инструментарий (скрипты wo-k8s), а деплой идёт в prod
                # дочерним билдом. Атрибуция по VCS-ветке приписывала такие
                # билды к preprod-* и squad-gd-* (у squad-gd правило ветки —
                # тоже preprod), а prod-* не получал ничего.
                # Инцидент 2026-08-11: Prod_BackupAllDb #40 (бэкап прод-БД перед
                # релизом) осел на squad-gd-shared/*. Для прод-конфигов ветка
                # неинформативна — окружение задаёт проект.
                branch_full = "prod"
            elif branch_full == "<default>":
                branch_full = "preprod"
            # Цель из параметров билда точнее ветки, и ветка здесь просто
            # врала. Замер 22.08.2026: `BuildAndDeploy #2917` деплоил squad-1
            # (NAMESPACE=squad-1), а по ветке `<default>`→preprod осел на
            # preprod-* и squad-gd-* — 596 записей, среди которых сервисов
            # squad-1 не было НИ ОДНОГО. `MigrateAndUpdateService #103`
            # деплоил chat-message-service в squad-27 и разошёлся по тем же
            # 596. Граф указывал не на те окружения, и в нужных деплоев не
            # было видно вообще.
            target_realm = b.get("target_realm")
            if target_realm:
                # NAMESPACE в TC — это РЕАЛЬМ: «squad-27», «preprod», «prod».
                # Разворачиваем в конкретные namespace графа по префиксу:
                # squad-27 → squad-27-shared, squad-27-kingdom2, ...
                prefix = f"{target_realm}-"
                target_namespaces = [
                    ns for ns in all_ns
                    if ns == target_realm or ns.startswith(prefix)
                ]
                attribution = "build_param"
                if not target_namespaces:
                    # Реальм есть, а namespace в графе нет — стенд снесён или
                    # ещё не отсканирован. Падать на ветку НЕЛЬЗЯ: она
                    # приписала бы деплой чужому окружению, а это хуже
                    # отсутствия записи.
                    logger.info(
                        "tc_deploys_to_kg.realm_not_in_graph realm=%s build=#%s",
                        target_realm, b.get("number"),
                    )
                    stats_skipped_no_realm += 1
                    continue
            else:
                target_namespaces = ns_by_branch.get(branch_full, [])
                attribution = "vcs_branch"
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
                q = db.query(Service).filter_by(namespace=ns, synthetic=False)
                target_service = b.get("target_service")
                if target_service:
                    # Конфиг деплоит ОДИН сервис — писать деплой на все 596
                    # значит утверждать 595 небылиц. Имя из SERVICE_NAME
                    # сверяется точно: если такого сервиса в namespace нет,
                    # записей просто не будет, и это честнее выдумки.
                    q = q.filter(Service.name == target_service)
                ns_services = q.all()
                for svc in ns_services:
                    try:
                        record_deployment(
                            db,
                            service=svc,
                            started_at=started_naive,
                            finished_at=finished_naive,
                            # sha/repo: A3-рерайт ns-wide broadcast (2026-05-24)
                            # потерял эти поля → kg_deployments.sha=NULL с 05-25.
                            # recent_deploys() их уже отдаёт; пробрасываем обратно.
                            sha=b.get("sha"),
                            repo=(b.get("all_revisions") or [{}])[0].get("root"),
                            buildtype_id=b.get("buildtype_id"),
                            build_number=str(b.get("number") or ""),
                            status=b.get("status"),
                            triggered_by=b.get("triggered_by"),
                            extras={
                                "branch": branch_full,
                                "buildtype_name": b.get("buildtype_name"),
                                "url": b.get("url"),
                                "namespace_scope": True,  # маркер ns-wide attribution
                                # ОТКУДА взялась привязка. Без этого поля
                                # счётчик by_attribution виден только в логах
                                # прогона, а по самим данным точную запись не
                                # отличить от догадки по ветке — потребителю
                                # приходится читать код, чтобы понять, можно
                                # ли доверять строке.
                                #
                                # `build_param` — цель из параметров билда
                                # (NAMESPACE/SERVICE_NAME), доверять можно.
                                # `vcs_branch` — вывод из ветки, а у
                                # deploy-конфигов она литеральный `<default>`:
                                # 22.08.2026 такая привязка отправила деплой
                                # squad-1 в preprod-* и squad-gd-*.
                                "attribution": attribution,
                                "target_realm": b.get("target_realm"),
                                "target_service": b.get("target_service"),
                                "all_revisions": b.get("all_revisions"),  # monorepo: все VCS root
                            },
                        )
                        added += 1
                        by_attribution[attribution] += 1
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
        "tc_deploys_to_kg.done builds=%d rows=%d by_param=%d by_branch=%d "
        "skipped_no_realm=%d at=%.0f",
        len(builds), added, by_attribution["build_param"],
        by_attribution["vcs_branch"], stats_skipped_no_realm, now_unix,
    )
    return {
        "builds_fetched": len(builds),
        # `rows_written`, а не `added`: record_deployment делает
        # on_conflict_do_update, и на неизменном наборе билдов счётчик
        # показывал одно и то же большое число каждые 15 минут (4768 при
        # восьми билдах). По нему нельзя было понять, пополняется граф или
        # просто перезаписывается — а именно этот вопрос и задают, когда
        # спрашивают «почему пайплайн не пополняет KG».
        "rows_written": added,
        "by_attribution": dict(by_attribution),
        "skipped_no_realm": stats_skipped_no_realm,
        # Оставлено для совместимости с потребителями старого ключа
        # (dashboard, digest): то же число, честное имя рядом.
        "kg_deployments_added": added,
    }


@celery_app.task(name="chronic_alerts_digest")
def chronic_alerts_digest_task():
    """L5: список «хронически тлеющих» сервисов в канал #stats.

    Гасит mute-эффект от L2 suppress-chronic. БЕЗ LLM — простой
    SQL-aggregate по kg_alerts + markdown через send_stats_report.
    """
    from app.services.chronic_digest import send_chronic_digest

    # Та же защита от idle-in-transaction, что у daily_stats_digest: SQL
    # перемежается с Discord I/O, по PG таск read-only.
    db = ReadOnlyAutocommitSession()
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
@single_instance(ttl_seconds=1800)
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
@single_instance(ttl_seconds=1800)
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
    finally:
        db.close()


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

    # AUTOCOMMIT-сессия: сборка дайджеста перемежает SQL с минутами VM/Discord
    # I/O; обычная сессия висит idle-in-transaction и убивается PG через 120с
    # (так дайджест молча пропадал 08-10.08.2026). По PG дайджест read-only.
    db = ReadOnlyAutocommitSession()
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


# Окно подавления — в Redis, общее для всех форков (app.core.fire_dedup).
# Раньше состояние жило в словаре модуля, и шестичасовое окно на практике
# держалось десятки минут: четыре форка вели независимые копии, а recycle по
# worker_max_memory_per_child обнулял их постоянно.
_SELF_HEALTH_DEDUP_CHANNEL = "self_health"
_SELF_HEALTH_DEDUP_SECONDS = 6 * 3600


async def _kg_self_health_logic() -> dict:
    from app.core.fire_dedup import mark_fired, should_fire
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
    if not should_fire(_SELF_HEALTH_DEDUP_CHANNEL, fp):
        logger.info("kg_self_health.discord_deduped fp=%s", fp)
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
        # Отмечаем ПОСЛЕ успешной отправки: упавший вебхук не должен
        # закрывать окно на шесть часов — иначе сбой доставки читается как
        # «уже сообщили».
        mark_fired(_SELF_HEALTH_DEDUP_CHANNEL, fp, _SELF_HEALTH_DEDUP_SECONDS)
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


# Подавление — в Redis, как и у self-health: см. app.core.fire_dedup.
_STUCK_ALERTS_DEDUP_CHANNEL = "stuck_alerts"


async def _kg_stuck_alerts_logic() -> dict:
    from app.core.fire_dedup import mark_fired, should_fire
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
    dedup_seconds = settings.STUCK_ALERTS_DEDUP_WINDOW_HOURS * 3600
    if not should_fire(_STUCK_ALERTS_DEDUP_CHANNEL, fp):
        logger.info("kg_stuck_alerts.discord_deduped fp=%s", fp)
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
        mark_fired(_STUCK_ALERTS_DEDUP_CHANNEL, fp, dedup_seconds)
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
@single_instance(ttl_seconds=1800)
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


@celery_app.task(name="kg_statics_versions_sync")
def kg_statics_versions_sync_task():
    """Statics version delta tracking → Redis (per ~5 мин).

    Для каждого env из STATICS_TRACK_ENVS наблюдает текущий номер версии
    статики и обновляет снимок `statics:seen:<env>` (см.
    statics_service.observe_statics_version). Держит «до»-снимок, чтобы
    enrichment мог отличить накат статики от cross-ns collateral. No-op
    если STATICS_* не настроен. Не raise — сбой tick'а не валит beat-loop.
    """
    from app.services.statics_service import observe_statics_version

    raw = getattr(settings, "STATICS_TRACK_ENVS", "") or ""
    envs = [e.strip() for e in raw.split(",") if e.strip()]
    if not envs:
        return {"skipped": "no_envs"}
    observed = 0
    changed = 0
    for env in envs:
        try:
            state = observe_statics_version(env)
        except Exception as e:
            logger.warning("kg_statics_versions_sync.observe_failed env=%s: %s", env, e)
            continue
        if not state:
            continue
        observed += 1
        # prev_version выставлен и first_observed_at свежий ⇒ на этом tick'е
        # зафиксирована смена версии (для observability лога).
        if state.get("prev_version") is not None:
            changed += 1
    logger.info(
        "kg_statics_versions_sync.done envs=%d observed=%d with_prev=%d",
        len(envs), observed, changed,
    )
    return {"envs": len(envs), "observed": observed, "with_prev": changed}


@celery_app.task(name="kg_nats_subjects_sync")
@single_instance(ttl_seconds=3600)
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


@celery_app.task(name="kg_ownership_backfill")
def kg_ownership_backfill_task():
    """Periodic backfill team_owner через multi-signal inference.

    Раз в 6h проходит по `kg_services` где team_owner NULL/'', прогоняет
    `suggest_owner_multi_signal` и UPDATE-ит owner при confidence >=
    OWNERSHIP_BACKFILL_THRESHOLD (default 0.7 — high-confidence only,
    чтобы не записывать спорные эвристики автоматом без review).

    Default OFF (OWNERSHIP_BACKFILL_ENABLED=false). Включается осознанно
    после dry-run прогона CLI: `python -m app.scripts.backfill_ownership`.
    Manual override (OWNERSHIP_MANIFEST_PATH=ownership.yaml) применяется
    всегда — manifest-confidence = 1.0.
    """
    if not getattr(settings, "OWNERSHIP_BACKFILL_ENABLED", False):
        logger.info("kg_ownership_backfill.skipped reason=disabled")
        return {"skipped": True, "reason": "OWNERSHIP_BACKFILL_ENABLED=false"}

    from app.scripts.backfill_ownership import run_backfill

    threshold = float(getattr(settings, "OWNERSHIP_BACKFILL_THRESHOLD", 0.7))
    db = SessionLocal()
    try:
        result = run_backfill(
            db,
            apply=True,
            threshold=threshold,
            do_ownership=True,
            do_stale=False,
        )
        logger.info(
            "kg_ownership_backfill.done updated=%d skipped_low_conf=%d kept=%d",
            result.actually_updated_owner,
            result.skipped_low_confidence,
            result.kept_existing,
        )
        return {
            "updated_owner": result.actually_updated_owner,
            "skipped_low_confidence": result.skipped_low_confidence,
            "kept_existing": result.kept_existing,
            "threshold": threshold,
        }
    except Exception as e:
        logger.warning("kg_ownership_backfill.failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


# ── Beat heartbeat tracking (для stats_digest.pipeline_health_section) ──────
#
# Pipeline gauge (см. stats_digest._record_task_heartbeat) различает
# «task ходит, данные stale» vs «task завис», читая Redis-heartbeat
# `stats:beat:last_run:<task_name>`. Этот сигнал пишет heartbeat ПОСЛЕ
# успешного завершения каждого beat-task'а из allowlist.
#
# Allowlist держим компактный — только тех, кого pipeline_health показывает:
#   kg_metrics_sync, kg_cluster_health_sync, kg_anomaly_detection_task,
#   kg_topology_sync, kg_seq_logs_sync, kg_signal_aggregates_compute.
# Остальным beat-task'ам heartbeat не критичен (есть отдельные observability
# каналы — KG таблицы, audit-log).
_BEAT_HEARTBEAT_TASKS = frozenset({
    "kg_metrics_sync",
    "kg_cluster_health_sync",
    "kg_anomaly_detection_task",
    "kg_topology_sync",
    "kg_seq_logs_sync",
    "kg_signal_aggregates_compute",
    # Ниже — источники отдельных видов рёбер. Их молчание агрегатная
    # проверка свежести (check_edges_freshness, порог 30% просроченных по
    # ВСЕМУ графу) заметить не может: на 14.08.2026 uses_nats это 20.8%
    # рёбер, serves_traffic — 31.2%, routes_to — 9.8%. То есть полная
    # остановка NATS- или ingress-синка не поднимала бы её порог никогда.
    # Heartbeat отвечает на другой вопрос, чем свежесть данных: «таск
    # вообще ходит» против «таск что-то записал».
    "kg_nats_subjects_sync",
    "kg_topology_resources_sync",
    "kg_ingress_sync",
    # Остальные источники данных, добавлены 23.08.2026 после ревизии
    # проверок. До неё все восемь были в расписании, но ни в sync_lag, ни
    # здесь: смерть любого из них не замечал никто.
    #
    # Хуже того, две проверки её активно маскировали:
    # `pod_events_link_rate` при нуле событий возвращает ok («нечего
    # связывать»), а `alerts_resolve_freshness` считает только открытые
    # алерты старше недели — если алерты перестанут приходить вовсе, их
    # число не вырастет. Обе честно отвечали на свой вопрос; вопроса
    # «а источник вообще жив» не задавал никто.
    #
    # Прецедент того же класса — `kg_seq_logs_sync` 20.08.2026: синк ходил,
    # NetworkPolicy рубила запросы, и 12,8 часа отчёт был «rows=0».
    "k8s_pod_events_sync",
    "kg_endpoints_sync",
    "kg_jobs_sync",
    "kg_storage_sync",
    "kg_statics_versions_sync",
    "kg_runtime_correlation_sync",
    "kg_ingress_observations_sync",
    "kg_alerts_resolve_sync",
    # Источник деплоев из кластера. Его молчание особенно незаметно: записи
    # из TeamCity продолжают идти, и kg_deployments выглядит живым.
    "kg_deploy_watch",
})


@task_postrun.connect
def _record_beat_heartbeat(sender=None, task_id=None, task=None, state=None, **kwargs):
    """Записать heartbeat для beat-task'а после успешного завершения.

    Не пишем для FAILURE — half-broken state как «свежий запуск» ввёл бы
    pipeline gauge в заблуждение. RETRY тоже игнорируем (Celery сделает
    новый postrun при успехе).

    Большинство kg_*-тасков глотают исключения и возвращают {"error": ...} —
    Celery считает такой прогон SUCCESS. Heartbeat за него писать НЕЛЬЗЯ:
    иначе digest.pipeline_health неделями рапортует «healthy» для синка,
    который каждый tick падает. Проверяем retval на error-маркеры.

    Fail-open: любая ошибка тут не должна валить task. Импорт ленивый,
    чтобы избежать circular на старте модуля.
    """
    try:
        task_name = getattr(task, "name", None) or sender
        if task_name not in _BEAT_HEARTBEAT_TASKS:
            return
        if state != "SUCCESS":
            return
        retval = kwargs.get("retval")
        if isinstance(retval, dict) and (
            retval.get("error") is not None or retval.get("status") == "error"
        ):
            return
        # Прогон пропущен singleton-локом (предыдущий экземпляр ещё идёт) —
        # это НЕ выполнение. Записать heartbeat означало бы отрапортовать
        # «синк живой» ровно в тот момент, когда он завис: deadman в
        # self_health смотрит именно на этот ключ.
        from app.workers.task_lock import is_skipped
        if is_skipped(retval):
            return
        # Lazy import — stats_digest тяжеловат на boot.
        from app.services.stats_digest import _record_task_heartbeat
        _record_task_heartbeat(task_name)
    except Exception as e:
        logger.warning("beat_heartbeat.write_failed: %s", e)
