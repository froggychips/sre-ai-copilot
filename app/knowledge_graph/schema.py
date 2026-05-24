"""SQLAlchemy схема knowledge-graph узлов и рёбер.

Все таблицы используют тот же Base/engine, что и IncidentRecord
(см. app/database.py) — одна БД, одна миграция Alembic.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, UniqueConstraint)
from sqlalchemy.orm import relationship

from app.database import Base


class Service(Base):
    """Узел графа: микросервис / deployment в k8s.

    `name` — стабильный slug (например, `town-service`), уникален в
    пределах namespace. Сервис может присутствовать в нескольких
    namespace (squad-1, squad-2) — это разные строки.
    """
    __tablename__ = "kg_services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    namespace = Column(String, nullable=False, index=True)
    team_owner = Column(String, nullable=True)   # squad, например `squad-gd`
    metadata_json = Column(JSON, nullable=True)  # labels, репо, runbook URL...
    # Synthetic = по дизайну никогда не имеет edges (cron-backups, nats-tools,
    # observability-exporters). Исключается из Orphan %-метрики в kg_quality.
    synthetic = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Service health score [0, 1] — composite (alerts × severity + crashloop +
    # recurrence). Recomputed периодически через `kg_health_recompute` beat task.
    # None = ещё не считалось. 1.0 = perfect health, 0.0 = down/broken.
    # Используется в kg_fragile_top для «истинного» ранжирования (не только
    # inbound count). Per ChatGPT review #4.3.
    health_score = Column(Float, nullable=True, index=True)
    health_computed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("namespace", "name", name="uq_kg_service_ns_name"),
    )


class Deployment(Base):
    """Узел графа: один rollout/build конкретного сервиса.

    Один сервис → много deployments в истории. Используется RecentDeployRule
    для «деплой за ≤60 минут до alert-а?».
    """
    __tablename__ = "kg_deployments"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    sha = Column(String, nullable=True)
    repo = Column(String, nullable=True)
    buildtype_id = Column(String, nullable=True)  # TeamCity build type
    build_number = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)        # SUCCESS / FAILURE / RUNNING
    triggered_by = Column(String, nullable=True)
    extras = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_deploy_service_time", "service_id", "started_at"),
    )


class AlertEvent(Base):
    """Узел графа: один alert от alertmanager.

    Дублирует часть данных IncidentRecord, но индексируется по
    (service_id, fired_at) — для запросов upstream/nearby по графу.
    """
    __tablename__ = "kg_alerts"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("kg_services.id"), nullable=True, index=True)
    alertname = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=True)
    fingerprint = Column(String, nullable=True, unique=True, index=True)
    fired_at = Column(DateTime, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
    incident_id = Column(String, nullable=True, index=True)  # связь с IncidentRecord
    raw = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_alert_service_time", "service_id", "fired_at"),
    )


class PodEvent(Base):
    """A4: k8s Event для pod-а (OOMKilled / FailedScheduling / ImagePullBackOff /
    FailedMount / BackOff / Unhealthy / NodeNotReady и т.п.).

    Источник — `kubectl get events` или `client.CoreV1Api.list_namespaced_event`.
    События k8s — параллельный signal к kg_alerts (которые приходят только
    из AlertManager и упускают диагностические события на pod-уровне).

    Dedup: уникальность по `event_uid` (k8s UID события). Один и тот же
    Event может быть прочитан несколько раз — пишем один раз, обновляем
    last_seen + count.
    """
    __tablename__ = "kg_pod_events"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("kg_services.id"), nullable=True, index=True)
    namespace = Column(String, nullable=False, index=True)
    pod_name = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=False, index=True)   # OOMKilled / FailedScheduling / ...
    message = Column(String, nullable=True)
    type = Column(String, nullable=True)                  # Warning / Normal
    event_uid = Column(String, nullable=False, unique=True, index=True)
    first_seen = Column(DateTime, nullable=False, index=True)
    last_seen = Column(DateTime, nullable=True)
    count = Column(Integer, nullable=True)                # сколько раз k8s видел event
    extras = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_pod_event_service_time", "service_id", "first_seen"),
        Index("ix_kg_pod_event_ns_reason_time", "namespace", "reason", "first_seen"),
    )


class ServiceHealth(Base):
    """Per-service snapshot из VictoriaMetrics (cpu/mem/restarts/5xx/p95).

    Записывается beat-task'ом `kg_metrics_sync` каждые ~10 мин. Уникальность
    по (service_id, ts) — повторный tick того же расписания не плодит дубли.
    FK без ondelete — случайная чистка services не должна снести историю.
    `source` различает откуда метрика взята: `vm` / `vm_kube_state` / etc.
    """
    __tablename__ = "kg_service_health"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("kg_services.id"), nullable=False, index=True,
    )
    ts = Column(DateTime, nullable=False)
    cpu_pct = Column(Float, nullable=True)
    mem_pct = Column(Float, nullable=True)
    restarts_rate = Column(Float, nullable=True)
    http_5xx_rate = Column(Float, nullable=True)
    p95_latency_ms = Column(Float, nullable=True)
    source = Column(String, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "ts", name="uq_kg_service_health_service_ts",
        ),
        Index("ix_kg_service_health_service_ts", "service_id", "ts"),
        Index("ix_kg_service_health_ts", "ts"),
    )


class ClusterObservation(Base):
    """Global cluster snapshot — те же поля что ClusterHealth.to_dict().

    Single row per ~5 минут (cron `kg_cluster_health_sync`). Используется для
    «trend last 24h» в digest'ах и как контекст для post-mortem (что было с
    cluster-ом на момент X). Уникальность по ts.
    """
    __tablename__ = "kg_cluster_observations"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, nullable=False)
    cpu_pct = Column(Float, nullable=True)
    mem_pct = Column(Float, nullable=True)
    disk_peak_pct = Column(Float, nullable=True)
    pods_running = Column(Integer, nullable=True)
    pods_pending = Column(Integer, nullable=True)
    pods_failed = Column(Integer, nullable=True)
    crashloops = Column(Integer, nullable=True)
    deploy_mismatch = Column(Integer, nullable=True)
    alerts_critical = Column(Integer, nullable=True)
    alerts_warning = Column(Integer, nullable=True)
    alerts_prod = Column(Integer, nullable=True)
    # Сырые поля из ClusterHealth — для forward-compat если VMClient добавит
    # новые сигналы, мы не теряем их даже без миграции.
    raw = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("ts", name="uq_kg_cluster_obs_ts"),
        Index("ix_kg_cluster_obs_ts", "ts"),
    )


class IngressObservation(Base):
    """Per ingress endpoint snapshot: p95/p99/rps/4xx/5xx.

    Источник host/path — synthetic-узлы `ingress:<host>` и edges в
    kg_service_edges (kind='calls', discovered_by='kg_sync/ingress'),
    backend service_id берётся из dst edge'а. Запись раз в ~10 мин beat-task'ом
    `kg_ingress_observations_sync`. Уникальность по (ingress_name, host, path, ts).
    """
    __tablename__ = "kg_ingress_observations"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, nullable=False)
    ingress_name = Column(String, nullable=False)
    host = Column(String, nullable=False)
    path = Column(String, nullable=True)
    service_id = Column(
        Integer, ForeignKey("kg_services.id"), nullable=True, index=True,
    )
    p95_latency_ms = Column(Float, nullable=True)
    p99_latency_ms = Column(Float, nullable=True)
    rps = Column(Float, nullable=True)
    error_5xx_rate = Column(Float, nullable=True)
    error_4xx_rate = Column(Float, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "ingress_name", "host", "path", "ts",
            name="uq_kg_ingress_obs_ingress_host_path_ts",
        ),
        Index("ix_kg_ingress_obs_ingress_ts", "ingress_name", "ts"),
    )


class SignalAggregate(Base):
    """Per-service агрегаты сигналов из САМОГО KG за окно window_hours.

    Считается из kg_deployments / kg_alerts / kg_pod_events beat-task'ом
    `kg_signal_aggregates_compute` (раз в час). Идемпотентно по
    (service_id, window_end). `slo_burn_pct` — упрощённо
    `alert_open_count_critical / max(1, deploy_count)`.
    """
    __tablename__ = "kg_signal_aggregates"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("kg_services.id"), nullable=False, index=True,
    )
    window_end = Column(DateTime, nullable=False)
    window_hours = Column(Integer, nullable=True)
    deploy_count = Column(Integer, nullable=True)
    deploy_failure_pct = Column(Float, nullable=True)
    alert_open_count = Column(Integer, nullable=True)
    alert_ttr_p50_min = Column(Float, nullable=True)
    pod_event_count = Column(Integer, nullable=True)
    top_event_reason = Column(String, nullable=True)
    slo_burn_pct = Column(Float, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "window_end",
            name="uq_kg_signal_aggregates_service_window",
        ),
        Index(
            "ix_kg_signal_aggregates_service_window",
            "service_id", "window_end",
        ),
    )


class AnomalyObservation(Base):
    """Per-service per-metric аномалия по rolling z-score (>3 sigma).

    Beat-task `kg_anomaly_detection_task` каждые ~10 мин пробегает по
    kg_service_health: текущая точка vs baseline (7d, исключая последний
    час). |z|>3 → 'warning'; |z|>5 → 'critical'. Идемпотентность по
    (service_id, ts, metric) — повторный tick тот же snapshot не дубль.

    `notified` — флаг для второй фазы (Discord). Default false, апдейт
    отдельно когда уведомление успешно отослано.
    """
    __tablename__ = "kg_anomaly_observations"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("kg_services.id"), nullable=False, index=True,
    )
    ts = Column(DateTime, nullable=False)
    metric = Column(String, nullable=False)
    value = Column(Float, nullable=True)
    baseline_mean = Column(Float, nullable=True)
    baseline_stddev = Column(Float, nullable=True)
    z_score = Column(Float, nullable=True)
    severity = Column(String, nullable=True)  # 'warning' | 'critical'
    notified = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    # JSON-debug: какой method использован (robust_z_flat/robust_z_seasonal),
    # сколько baseline-точек, MAD, threshold-config. Не используется для
    # фильтрации — только для post-mortem'ов и tuning'а.
    extras = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "ts", "metric",
            name="uq_kg_anomaly_obs_service_ts_metric",
        ),
        Index("ix_kg_anomaly_obs_service_ts", "service_id", "ts"),
        Index("ix_kg_anomaly_obs_severity_ts", "severity", "ts"),
    )


class LogObservation(Base):
    """Per-service агрегат error/fatal/warning логов из Seq за окно.

    Beat-task `kg_seq_logs_sync` каждые ~10 мин тянет count событий по
    level=Error/Fatal/Warning из нескольких Seq-инстансов (prod / preprod /
    preupdate) и пишет одну строку per (service, level, source) в окно.

    `service_id` NULLABLE — если по Application-тэгу из Seq не получилось
    сматчить запись в `kg_services` (новый сервис ещё не в KG или
    нестандартный тэг), всё равно сохраняем aggregate с service_id=NULL.
    Атрибуция остаётся через `namespace` + `source` (имя Seq-инстанса).

    `top_message_hash` — md5 от самого частого MessageTemplate за окно.
    Стабильный fingerprint для группировки: msg повторяется три тика
    подряд — значит это chronic-pattern. `sample_message` — текстовый
    пример топа.

    Идемпотентность: UNIQUE(service_id, ts, level, source); повторный
    beat-tick в том же окне делает ON CONFLICT DO UPDATE count=excluded.count
    в `seq_logs_sync.py`.
    """
    __tablename__ = "kg_log_observations"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("kg_services.id"), nullable=True, index=True,
    )
    ts = Column(DateTime, nullable=False)
    # Error / Fatal / Warning — Seq использует эти строковые уровни.
    level = Column(String, nullable=False)
    count = Column(Integer, nullable=False)
    top_message_hash = Column(String, nullable=True)
    sample_message = Column(String, nullable=True)
    # Имя Seq-инстанса: prod / preprod / preupdate / wo-api3-prod / ...
    source = Column(String, nullable=True)
    namespace = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "ts", "level", "source",
            name="uq_kg_log_obs_service_ts_level_source",
        ),
        Index("ix_kg_log_obs_service_ts", "service_id", "ts"),
        Index("ix_kg_log_obs_level_ts", "level", "ts"),
    )


class ActionApproval(Base):
    """Persistent approve/decline решение по proposed action из Discord embed.

    Создаётся при клике Approve/Decline-кнопки в incident-embed. UNIQUE по
    (incident_id, intent_signature) — повторный клик ловится коллизией и
    handler отвечает "already approved/declined by @user".

    `intent_signature` — детерминированный хэш ExecutionIntent
    (action+resource+ns+params), вычисляется через
    `app.services.intent_signature.compute_signature`. Не sequence-номер:
    одна команда — одна approval-запись.

    `status` финальное: `approved` | `declined`. PENDING-промежутка нет —
    кнопка либо нажата (row создаётся), либо нет.

    `approved_by` — Discord username/id того кто нажал. Для audit-трейла.
    """
    __tablename__ = "kg_action_approvals"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=True)            # ActionType value, для quick-filter
    intent_signature = Column(String, nullable=False)
    status = Column(String, nullable=False)           # "approved" | "declined"
    approved_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "intent_signature",
            name="uq_kg_action_approvals_incident_intent",
        ),
        Index(
            "ix_kg_action_approvals_status_decided",
            "status", "decided_at",
        ),
    )


class K8sJob(Base):
    """KG Coverage #1: узел графа для k8s Job или CronJob.

    Один Service может иметь несколько связанных CronJob-ов (backup +
    cleanup + reindex), плюс ad-hoc Job-ы (migration на каждый rollout).
    Хранить их в `kg_services` смешало бы health-метрики (Job завершается —
    Service остаётся). Поэтому отдельная table.

    `kind` различает Job / CronJob.

    Для CronJob: `schedule` (cron-выражение) + `last_schedule_time` +
    `last_successful_time` + `suspended`. Эти поля позволяют детектить:
        * cron не запускался N дней
        * `last_successful_time` далеко позади `last_schedule_time`
          (последний запуск свалился)
        * suspended=true (намеренно остановлен — это feature, не алёрт)

    Для Job: `succeeded_count` / `failed_count` / `active_count` +
    `last_pod_exit_code` (из последнего pod-а). Failed alembic-migration с
    exit_code=1 — это критичный сигнал для backfill RecentDeployRule.

    `owner_service_name` — label-attribution на Service в kg_services
    (тот же namespace). Если match сработал, в metadata_json также
    кладётся `owner_service_id` для O(1) join'а. Для Job, созданного
    CronJob-ом, дополнительно проставляется через ownerReferences
    transitive resolve в `k8s_jobs_sync._link_jobs_to_cronjob_owners`.
    """
    __tablename__ = "kg_k8s_jobs"

    id = Column(Integer, primary_key=True, index=True)
    namespace = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    # 'job' | 'cronjob'. Hard-coded enum как и в discovery_sources —
    # валидация на app-уровне, сейчас плоская string.
    kind = Column(String, nullable=False, index=True)

    # Owner-label attribution. `owner_service_name` — name из k8s label
    # (`app.kubernetes.io/part-of` или `app`); `owner_service_id` живёт в
    # metadata_json — это сматченный id из kg_services. Опционально.
    owner_service_name = Column(String, nullable=True, index=True)

    # CronJob-only поля. На Job-узлах остаются None.
    schedule = Column(String, nullable=True)              # cron expression
    suspended = Column(Boolean, nullable=False, default=False, server_default="false")
    last_schedule_time = Column(DateTime, nullable=True)
    last_successful_time = Column(DateTime, nullable=True)

    # Job-counters. На CronJob: active_count = len(status.active), succeeded/
    # failed остаются 0 (это для одного Job-а). UX-ясности это не вредит:
    # фильтрация по kind отделяет одно от другого.
    succeeded_count = Column(Integer, nullable=True)
    failed_count = Column(Integer, nullable=True)
    active_count = Column(Integer, nullable=True)
    start_time = Column(DateTime, nullable=True)
    completion_time = Column(DateTime, nullable=True)

    # Job-only: exit-code из последнего terminated container первого pod-а.
    # NULL если pod ещё running или failed_count=0 (мы не дёргаем exit-code
    # на success — implied 0). Используется для post-mortem: «alembic
    # migration упала с exit 1 за час до alert KubeDeploymentReplicasMismatch».
    last_pod_exit_code = Column(Integer, nullable=True)

    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Refresh timestamp на каждом upsert. Stale rows (не sync N часов) —
    # кандидаты на drift_cleanup. Не индексируем — выборка raredата-аналитики.
    last_seen_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("namespace", "name", "kind", name="uq_kg_k8s_job_ns_name_kind"),
        Index("ix_kg_k8s_job_kind_ns", "kind", "namespace"),
    )


class ServiceEdge(Base):
    """Ребро графа: src сервис вызывает / зависит от dst сервиса.

    `kind` — тип зависимости:
        * `calls`        — синхронный HTTP/gRPC
        * `consumes`     — kafka/queue topic
        * `reads_from`   — DB / cache
        * `runs_on`      — pod на этой ноде / cluster
    Граф направленный: A `calls` B → «A падает, если B недоступен».
    upstream_of(A) = сервисы, от которых A зависит = dst у edge с src=A.
    """
    __tablename__ = "kg_service_edges"

    id = Column(Integer, primary_key=True, index=True)
    src_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    dst_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    weight = Column(Integer, default=1)          # «жирность» edge: % трафика, важность
    discovered_by = Column(String, nullable=True)  # populator/method, для отладки
    extras = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # C1: refresh timestamp. Каждый upsert_edge → last_seen_at = now().
    # Edges, не подтверждённые за N дней — кандидаты на soft-cleanup
    # (см. queries.upstream_of(..., fresh_only=True) и beat-task
    # kg_edges_decay в будущем).
    last_seen_at = Column(DateTime, nullable=True, index=True)

    src = relationship("Service", foreign_keys=[src_id])
    dst = relationship("Service", foreign_keys=[dst_id])

    __table_args__ = (
        UniqueConstraint("src_id", "dst_id", "kind", name="uq_kg_edge_src_dst_kind"),
    )
