"""SQLAlchemy схема knowledge-graph узлов и рёбер.

Все таблицы используют тот же Base/engine, что и IncidentRecord
(см. app/database.py) — одна БД, одна миграция Alembic.
"""
from __future__ import annotations

from datetime import datetime

from typing import Any, Optional
from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.knowledge_graph.contract import UQ_KG_SERVICE_NS_NAME_KIND


# ── Типы узлов графа ────────────────────────────────────────────────────────
#
# До 07.08.2026 таблица kg_services хранила ВСЁ одним типом, и k8s Service
# схлопывался с k8s Deployment по (namespace, name): `auth-service` — один
# узел. Из-за этого ребро serves_traffic (Service → backing workload) было
# невозможно построить в принципе — оно всегда получалось self-loop.
# Замер на живом графе: 2092 self-loop и 2231 no_match отбрасывались каждый
# тик, а рёбер serves_traffic оставалось ровно 3.
#
# Слово «deployment» в этой кодовой базе уже занято: класс Deployment ниже —
# это rollout/build из TeamCity (kg_deployments), а не k8s-объект. Поэтому
# исполняемая сущность называется workload — заодно покрывает StatefulSet и
# DaemonSet, которые тоже стоят за Service.
NODE_KIND_SERVICE = "service"    # k8s Service / логическая точка входа
NODE_KIND_WORKLOAD = "workload"  # k8s Deployment / StatefulSet / DaemonSet
NODE_KIND_INGRESS = "ingress"    # synthetic-узел ingress:<name>
NODE_KINDS = (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD, NODE_KIND_INGRESS)


#: Состояния жизненного цикла namespace (колонка kg_namespaces.state).
#: Семантика и переходы — docs/KG_SCHEMA_CONTRACT.md.
NS_STATE_ACTIVE = "active"      # namespace виден в кластере
NS_STATE_MISSING = "missing"    # исчез, но ещё не забыт
NS_STATE_RETIRED = "retired"    # забыт: узлы и история удалены
NS_STATES = (NS_STATE_ACTIVE, NS_STATE_MISSING, NS_STATE_RETIRED)


class Namespace(Base):
    """Namespace как объект со своей жизнью, а не строка-текст в kg_services.

    До появления этой таблицы граф знал про namespace только его имя. Отсюда
    три поломки сразу:

      * **выход не отслеживался** — узлы снесённого стенда оставались навсегда
        (198 namespace в графе против 139 живых на 14.08.2026);
      * **пересоздание было невидимо** — сквад сносят и раскатывают заново под
        тем же именем, а upsert по (namespace, name, node_kind) попадает в
        СТАРУЮ строку: к новому стенду прилипают health-точки, алерты и
        `created_at` предыдущего. Замер: `squad-1-shared` — узлы на 82 дня
        старше самого namespace, 39 775 health-точек прошлой жизни;
      * **уборка блокировала сама себя** — guard `drift_pct > 20%` считал долю
        и переставал работать ровно тогда, когда мусора накопилось больше
        всего (29.8% при пороге 20%).

    Идентичность инкарнации — `k8s_uid`. Ключ остался именем: по нему идут все
    существующие джойны, а UID хранится атрибутом. Смена UID при том же имени =
    новая инкарнация (`incarnation` инкрементится).
    """

    __tablename__ = "kg_namespaces"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    #: metadata.uid namespace в кластере. NULL у строк, заведённых до того,
    #: как синк начал его читать — для них инкарнация неизвестна.
    k8s_uid: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    #: metadata.creationTimestamp — возраст САМОГО namespace, в отличие от
    #: created_at узлов, который у пересозданного стенда врёт.
    k8s_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Счётчик пересозданий: 1 у первой известной инкарнации.
    incarnation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    state: Mapped[str] = mapped_column(
        String, nullable=False, index=True,
        default=NS_STATE_ACTIVE, server_default=NS_STATE_ACTIVE,
    )
    #: Когда граф впервые увидел ЭТУ инкарнацию (не namespace вообще).
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    #: Последний тик, в котором namespace был жив в кластере.
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    #: Когда впервые не увидели. NULL у active — по нему считается TTL до
    #: retired, то есть время, а не доля, решает судьбу узлов.
    missing_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    #: Сырые лейблы namespace (`deployed-by` / `deployed-branch`) — что
    #: поставил TeamCity при раскатке. Пишет lifecycle каждым тиком. Это ещё
    #: не владелец: кнопку мог нажать сервисный аккаунт (ai-agent, cicd), а
    #: ветку — раскатать коллега для проверки чужой задачи.
    deployed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    deployed_branch: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Лейбл `squad-owner`: кто занял стенд кнопкой ГД. Отличается от
    # deployed_by, который остаётся от прежнего деплойера стенда.
    claim_owner: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Владелец стенда — человек, к которому идти с вопросом «твой стенд
    #: болеет». Резолв (`kg_namespace_owner_sync`): ручной override →
    #: assignee Jira-задачи из ветки → deployed-by, если это не сервисный
    #: аккаунт → кто триггерил последний деплой в TC. `owner_source` хранит,
    #: какой из путей сработал (contract.NAMESPACE_OWNER_SOURCES).
    owner_login: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    owner_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    owner_jira_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Discord snowflake для @mention — из манифеста людей
    #: (PEOPLE_MANIFEST_PATH), в код не попадает.
    owner_discord_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    owner_resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Последняя игровая сессия по ClickHouse сквада (опционально). Критерий
    #: «стенд простаивает» для медика — вместо stale_class, который для
    #: сквадов не работает (их деплои — ns-broadcast, active недостижим).
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Service(Base):
    """Узел графа: сервис, workload или ingress — см. `node_kind`.

    `name` — стабильный slug (например, `town-service`), уникален в
    пределах namespace И типа узла. Сервис может присутствовать в нескольких
    namespace (squad-1, squad-2) — это разные строки.
    """
    __tablename__ = "kg_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Без `index=True`: uq_kg_service_ns_name_kind (namespace, name,
    # node_kind) покрывает эту колонку как префикс.
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    # Тип узла. Существующие строки мигрированы в 'service' — то есть смысл
    # старых данных не меняется, а k8s-workload'ы теперь заводятся отдельными
    # узлами и перестают конфликтовать с одноимённым Service.
    node_kind: Mapped[str] = mapped_column(
        String, nullable=False, index=True,
        default=NODE_KIND_SERVICE, server_default=NODE_KIND_SERVICE,
    )
    team_owner: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # squad, например `squad-gd`
    # Откуда взялся team_owner. Значения — contract.OWNER_SOURCES.
    # NULL = провенанс неизвестен (строки до 14.08.2026 и любой источник,
    # который его ещё не проставляет). Смысл: префиксная эвристика ошибается
    # на переименованиях, k8s-лейбл не врёт — а без этой колонки оба выглядят
    # одинаково, и «12 577 узлов с владельцем» читается как 12 577 надёжных.
    # Без index: значений всего шесть (contract.OWNER_SOURCES), и единственный
    # сценарий чтения — агрегат «сколько узлов по каждому источнику». На такой
    # селективности PostgreSQL всё равно выберет seq scan, а лишний индекс на
    # kg_services — это ещё одна структура, которую ALTER TABLE будет блокировать.
    owner_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)  # labels, репо, runbook URL...
    # Synthetic = по дизайну никогда не имеет edges (cron-backups, nats-tools,
    # observability-exporters). Исключается из Orphan %-метрики в kg_quality.
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Service health score [0, 1] — composite (alerts × severity + crashloop +
    # recurrence). Recomputed периодически через `kg_health_recompute` beat task.
    # None = ещё не считалось. 1.0 = perfect health, 0.0 = down/broken.
    # Используется в kg_fragile_top для «истинного» ранжирования (не только
    # inbound count). Per ChatGPT review #4.3.
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    health_computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # KG Coverage #4 (2026-05-24): first-class stale классификация.
    # Значения: 'active' | 'expected_stale' | 'suspicious_stale'.
    # Заполняется в `kg_sync.sync_namespace` на каждом ран-е (idempotent) и
    # читается из `stats_digest.stale_deployments_section` / dashboards.
    # Реализация эвристики — `app/knowledge_graph/stale_classifier.py`.
    stale_class: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)

    # ── Идентичность объекта, а не его имени ─────────────────────────────
    #
    # До 05.09.2026 узел графа опознавался тройкой (namespace, name,
    # node_kind), и этого хватало ровно до первого пересоздания. Снесённый и
    # заведённый заново Deployment — другой объект k8s с другим `uid`, но для
    # графа он неотличим от прежнего: к нему прирастает вся старая история —
    # деплои, алерты, health, рёбра.
    #
    # У namespace эта проблема решена с 14.08.2026 (`kg_namespaces.k8s_uid` +
    # `incarnation` + `namespace_lifecycle`); у workload'ов — нет, хотя
    # пересоздают их на порядок чаще: каждый `--wipe` сквада, каждая смена
    # селектора, каждый helm uninstall/install.
    #
    # NULL здесь — «источник не сообщил uid», а не «объект без uid»: узлы из
    # алертов и ingress-синтетики заводятся без обхода k8s API.
    k8s_uid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Порядковый номер воплощения. Растёт, когда под тем же именем появился
    # объект с другим `k8s_uid`. Смысл тот же, что у `kg_namespaces`.
    incarnation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Когда инкарнация сменилась в последний раз. Без метки факт пересоздания
    # виден только как «число стало 2» — без ответа, когда именно, а значит
    # и без возможности связать его с инцидентом.
    incarnation_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # node_kind в ключе: Service и workload с одинаковым именем — это
        # РАЗНЫЕ узлы, иначе serves_traffic снова схлопнется в self-loop.
        UniqueConstraint(
            # Имя — из contract.py: тот же литерал читает единственный upsert,
            # поэтому переименование констрейнта не может разъехаться с
            # `ON CONFLICT` (регрессия #245).
            "namespace", "name", "node_kind", name=UQ_KG_SERVICE_NS_NAME_KIND,
        ),
    )


class Deployment(Base):
    """Узел графа: один rollout/build конкретного сервиса.

    Один сервис → много deployments в истории. Используется RecentDeployRule
    для «деплой за ≤60 минут до alert-а?».
    """
    __tablename__ = "kg_deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: (service_id, started_at) ниже покрывает его как
    # префикс — отдельный индекс делал бы ту же работу второй раз.
    service_id: Mapped[int] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=False)
    sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    repo: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    buildtype_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # TeamCity build type
    build_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)        # SUCCESS / FAILURE / RUNNING
    triggered_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_deploy_service_time", "service_id", "started_at"),
        # Один build (service + buildtype + build_number) — одна строка.
        # Раньше дедуп был только check-then-insert в record_deployment,
        # и конкурентные вызовы (beat task + incident pipeline) плодили
        # дубли, раздувая deploy_count / deploy_failure_pct. NULL-значения
        # buildtype/build_number в PG считаются различными — деплои без
        # build-инфо (только started_at) констрейнт не ограничивает.
        UniqueConstraint(
            "service_id", "buildtype_id", "build_number",
            name="uq_kg_deploy_service_build",
        ),
    )


class AlertEvent(Base):
    """Узел графа: один alert от alertmanager.

    Дублирует часть данных IncidentRecord, но индексируется по
    (service_id, fired_at) — для запросов upstream/nearby по графу.
    """
    __tablename__ = "kg_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: ix_kg_alert_service_time (service_id, fired_at) покрывает эту колонку как префикс.
    service_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=True)
    alertname: Mapped[str] = mapped_column(String, nullable=False, index=True)
    severity: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True, index=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # Последний раз, когда AM прислал webhook по этому alert-у (repeat_interval).
    # Для хронических алертов (fired_at недели назад) именно это поле попадает
    # в окно деплоя и используется в deploy_incident_correlation_section.
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    incident_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)  # связь с IncidentRecord
    raw: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: ix_kg_pod_event_service_time (service_id, first_seen) покрывает эту колонку как префикс.
    service_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=True)
    # Без `index=True`: ix_kg_pod_event_ns_reason_time (namespace, reason,
    # first_seen) покрывает эту колонку как префикс.
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    pod_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, index=True)   # OOMKilled / FailedScheduling / ...
    message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    type: Mapped[Optional[str]] = mapped_column(String, nullable=True)                  # Warning / Normal
    event_uid: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)                # сколько раз k8s видел event
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
    # Без `index=True`: uq_kg_service_health_service_ts (service_id, ts) покрывает эту колонку как префикс.
        Integer, ForeignKey("kg_services.id"), nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cpu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    restarts_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    http_5xx_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p95_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Здоровье Orleans-силоса (v1.0.9). Источник — pull-скрейп `/metrics`
    # town-grainhost'а (VMPodScrape в чарте, метер Microsoft.Orleans через
    # prometheus-net). Отдельная семья колонок, а не подмена http_*/p95:
    # это grain-вызовы и membership силоса, а не HTTP RED, и латентность
    # здесь средняя (count+sum), гистограммы в скрейпе нет. NULL — у сервиса
    # нет Orleans-метрик вовсе; 0.0 у счётчиков сбоев — «есть силос, сбоев
    # не было» (prometheus-net не отдаёт счётчик до первого инкремента).
    orleans_latency_avg_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    orleans_timedout_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # /мин
    orleans_messaging_fault_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # rejected+expired+sent_failed+sent_dropped, /мин (rerouted — не сбой, см. metrics_sync)
    orleans_pings_missed_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)     # pings_reply_missed, /мин — прокси death-vote
    orleans_rerouted_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)         # messaging_rerouted, /мин — пересылка на другой силос: не сбой, но растёт раньше сбоев (1.0.11)
    orleans_activation_churn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # created+destroyed+shutdown, /мин
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "ts", name="uq_kg_service_health_service_ts",
        ),
        # (service_id, ts) отдельным индексом не объявляем: ровно этот
        # состав уже держит UniqueConstraint выше, и обычная копия стоила
        # 928 МБ и одного лишнего обновления на каждую из 377 тысяч строк в
        # сутки. По той же причине у `service_id` нет `index=True` — он
        # префикс уникального.
        Index("ix_kg_service_health_ts", "ts"),
    )


class ClusterObservation(Base):
    """Global cluster snapshot — те же поля что ClusterHealth.to_dict().

    Single row per ~5 минут (cron `kg_cluster_health_sync`). Используется для
    «trend last 24h» в digest'ах и как контекст для post-mortem (что было с
    cluster-ом на момент X). Уникальность по ts.
    """
    __tablename__ = "kg_cluster_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cpu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_peak_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pods_running: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pods_pending: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pods_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    crashloops: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deploy_mismatch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alerts_critical: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alerts_warning: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alerts_prod: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Сырые поля из ClusterHealth — для forward-compat если VMClient добавит
    # новые сигналы, мы не теряем их даже без миграции.
    raw: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("ts", name="uq_kg_cluster_obs_ts"),
        # `ts` уже уникален (см. UniqueConstraint выше) — копии не нужно.
    )


class IngressObservation(Base):
    """Per ingress endpoint snapshot: p95/p99/rps/4xx/5xx.

    Источник host/path — synthetic-узлы `ingress:<host>` и edges в
    kg_service_edges (kind='calls', discovered_by='kg_sync/ingress'),
    backend service_id берётся из dst edge'а. Запись раз в ~10 мин beat-task'ом
    `kg_ingress_observations_sync`. Уникальность по (ingress_name, host, path, ts).
    """
    __tablename__ = "kg_ingress_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingress_name: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    service_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("kg_services.id"), nullable=True, index=True,
    )
    p95_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p99_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_5xx_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_4xx_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
    # Без `index=True`: uq_kg_signal_aggregates_service_window (service_id, window_end) покрывает эту колонку как префикс.
        Integer, ForeignKey("kg_services.id"), nullable=False,
    )
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deploy_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deploy_failure_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    alert_open_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alert_ttr_p50_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pod_event_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    top_event_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slo_burn_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "window_end",
            name="uq_kg_signal_aggregates_service_window",
        ),
        # (service_id, window_end) держит UniqueConstraint выше.
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
    # Без `index=True`: uq_kg_anomaly_obs_service_ts_metric (service_id, ts, metric) покрывает эту колонку как префикс.
        Integer, ForeignKey("kg_services.id"), nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metric: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline_mean: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline_stddev: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    z_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # 'warning' | 'critical'
    notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    # JSON-debug: какой method использован (robust_z_flat/robust_z_seasonal),
    # сколько baseline-точек, MAD, threshold-config. Не используется для
    # фильтрации — только для post-mortem'ов и tuning'а.
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "ts", "metric",
            name="uq_kg_anomaly_obs_service_ts_metric",
        ),
        # (service_id, ts) — префикс уникального (service_id, ts, metric),
        # b-tree обслуживает такие запросы им же.
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

    Идемпотентность: UNIQUE(ts, level, source, app_name); повторный
    beat-tick в том же окне делает ON CONFLICT DO UPDATE count=excluded.count
    в `seq_logs_sync.py`.

    История ключа: раньше ключ был (service_id, ts, level, source), но
    service_id NULLABLE — в PG NULL-ы различны, и для несматченных сервисов
    конфликт НЕ срабатывал вовсе (каждый retry/tick плодил дубли). Плюс два
    разных Seq `App`-а, резолвящихся в один сервис, коллизили в одну строку
    и затирали count друг друга. Теперь идентичность строки — сырое имя
    приложения из Seq (`app_name`, NOT NULL), а service_id — деривированная
    атрибуция. Один App → одна строка на окно; два App-а одного сервиса —
    две строки, суммирование делают консьюмеры (SUM(count) в queries).
    """
    __tablename__ = "kg_log_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[Optional[int]] = mapped_column(
    # Без `index=True`: ix_kg_log_obs_service_ts (service_id, ts) покрывает эту колонку как префикс.
        Integer, ForeignKey("kg_services.id"), nullable=True,
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Error / Fatal / Warning — Seq использует эти строковые уровни.
    level: Mapped[str] = mapped_column(String, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    top_message_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sample_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Имя Seq-инстанса: prod / preprod / preupdate / wo-api3-prod / ...
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    namespace: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Сырой `App`-тэг из Seq — детерминированная NOT NULL часть UNIQUE-ключа
    # (вместо NULLABLE service_id, см. docstring). Legacy-строки бэкфиллятся
    # суррогатами `legacy-svc:<service_id>` / `legacy:<id>` в миграции
    # 20260807_0400 (kg_idempotency_constraints, пункт 2) — без потери данных.
    # Ссылка была на 20260807_0200 (add_node_kind) — та к app_name отношения
    # не имеет; при инцидентном откате это направляло оператора не туда.
    app_name: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint(
            "ts", "level", "source", "app_name",
            name="uq_kg_log_obs_ts_level_source_app",
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: uq_kg_action_approvals_incident_intent
    # (incident_id, intent_signature) покрывает эту колонку как префикс.
    incident_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[Optional[str]] = mapped_column(String, nullable=True)            # ActionType value, для quick-filter
    intent_signature: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)           # "approved" | "declined"
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: uq_kg_k8s_job_ns_name_kind (namespace, name, kind)
    # покрывает эту колонку как префикс.
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 'job' | 'cronjob'. Hard-coded enum как и в discovery_sources —
    # валидация на app-уровне, сейчас плоская string.
    # Без `index=True`: ix_kg_k8s_job_kind_ns (kind, namespace) покрывает
    # эту колонку как префикс.
    kind: Mapped[str] = mapped_column(String, nullable=False)

    # Owner-label attribution. `owner_service_name` — name из k8s label
    # (`app.kubernetes.io/part-of` или `app`); `owner_service_id` живёт в
    # metadata_json — это сматченный id из kg_services. Опционально.
    owner_service_name: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)

    # CronJob-only поля. На Job-узлах остаются None.
    schedule: Mapped[Optional[str]] = mapped_column(String, nullable=True)              # cron expression
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_schedule_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_successful_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Job-counters. На CronJob: active_count = len(status.active), succeeded/
    # failed остаются 0 (это для одного Job-а). UX-ясности это не вредит:
    # фильтрация по kind отделяет одно от другого.
    succeeded_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failed_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completion_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Job-only: exit-code из последнего terminated container первого pod-а.
    # NULL если pod ещё running или failed_count=0 (мы не дёргаем exit-code
    # на success — implied 0). Используется для post-mortem: «alembic
    # migration упала с exit 1 за час до alert KubeDeploymentReplicasMismatch».
    last_pod_exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    metadata_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    # Refresh timestamp на каждом upsert. Stale rows (не sync N часов) —
    # кандидаты на drift_cleanup. Не индексируем — выборка raredата-аналитики.
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("namespace", "name", "kind", name="uq_kg_k8s_job_ns_name_kind"),
        Index("ix_kg_k8s_job_kind_ns", "kind", "namespace"),
    )


class K8sJobRun(Base):
    """История состояний Job-а: строка на каждое изменение статуса.

    `kg_k8s_jobs` — снимок последнего состояния, и он теряет ровно то, что
    нужно при разборе: упавший migrate-job к моменту разбора перезаписан
    следующим запуском с тем же именем или удалён вместе с namespace-ом.
    Замер 24.09.2026: у squad-19 на 12–14.09 (phantom-миграции после wipe) в
    графе остались строки только от 31.08 — модель видела «под крашится», а
    «migrate-job упал с BackoffLimitExceeded» уже не видел никто.

    Пишет `k8s_jobs_sync` (см. `k8s_job_history.record_job_run`), только когда
    состояние отличается от последней записи того же Job-а: Job с тем же
    именем, но другим `uid` — новый запуск. Читает `jobs_state_at` —
    состояние Job-ов namespace-ов на момент T. Retention —
    `KG_K8S_JOB_RUNS_RETENTION_DAYS` (30 дней, как у событий подов).
    """
    __tablename__ = "kg_k8s_job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # metadata.uid — отличает перезапуск с тем же именем от обновления статуса.
    uid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    owner_service_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    succeeded_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failed_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completion_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_pod_exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Терминальное условие Job-а: type (Complete / Failed / SuccessCriteriaMet
    # / FailureTarget), reason (BackoffLimitExceeded / DeadlineExceeded) и
    # обрезанное message. Для активного Job-а — NULL.
    condition_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    condition_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    condition_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Job пропал из кластера (удалён, TTL, снос namespace-а): tombstone-строка
    # с последним известным состоянием. Без неё последний «running» жил бы в
    # истории вечно — sync больше этот Job не видит и ничего не пишет.
    disappeared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Когда sync увидел это состояние: верхняя граница «с какого момента
    # известно», точность — период sync-а (15 мин).
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_kg_k8s_job_runs_ns_observed", "namespace", "observed_at"),
        Index("ix_kg_k8s_job_runs_ns_name", "namespace", "name", "id"),
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    src_id: Mapped[int] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    dst_id: Mapped[int] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Направление для kind'ов где оно различает РАЗНЫЕ рёбра (uses_nats:
    # `pub` / `sub`). Для остальных kinds — пустая строка (NOT NULL, чтобы
    # UNIQUE-конфликт срабатывал: NULL-ы в PG различны). Раньше direction
    # жил только в extras и pub+sub схлопывались в одно ребро с
    # flip-flop'ом направления между тиками.
    direction: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    weight: Mapped[Optional[int]] = mapped_column(Integer, default=1)          # «жирность» edge: % трафика, важность
    discovered_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # populator/method, для отладки
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    # C1: refresh timestamp. Каждый upsert_edge → last_seen_at = now().
    # Edges, не подтверждённые за N дней — кандидаты на soft-cleanup
    # (см. queries.upstream_of(..., fresh_only=True) и beat-task
    # kg_edges_decay в будущем).
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    src = relationship("Service", foreign_keys=[src_id])
    dst = relationship("Service", foreign_keys=[dst_id])

    __table_args__ = (
        UniqueConstraint(
            "src_id", "dst_id", "kind", "direction",
            name="uq_kg_edge_src_dst_kind_direction",
        ),
    )


class StorageVolume(Base):
    """Узел графа для k8s PVC и PV (KG Coverage #2).

    `kind`:
        * 'pvc' — PersistentVolumeClaim, namespace-scoped. `namespace` =
          реальный ns; `volume_name` указывает на bound PV (если Bound).
        * 'pv' — PersistentVolume, cluster-scoped. `namespace` = ''
          (пустая строка, не NULL — упрощает JOIN/UNIQUE).

    Не reuse `kg_services` потому что:
      * жизненный цикл другой (claim/release/bound vs deployment rollout);
      * drift_cleanup logically scoped на deployments — мы не хотим чтобы
        Pending PVC были помечены synthetic;
      * атрибуты другие (capacity, storage_class, phase).

    `disk_pct` — последний known % использования из kubelet_volume_stats_*
    (PromQL `100 * used_bytes / capacity_bytes`). NULL когда:
      * STORAGE_METRICS_ENABLED=False (default);
      * scrape config не покрывает kubelet stats (см. WO VM scrape gap recon);
      * volume только что создан и метрика ещё не пришла.

    `volume_name` дублирует bound_to edge для быстрых выборок «какой PV у
    этого PVC» без JOIN-а. Edge остаётся source of truth — колонка
    обновляется в том же transaction.
    """
    __tablename__ = "kg_storage_volumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Без `index=True`: ix_kg_storage_volumes_kind_ns (kind, namespace)
    # покрывает эту колонку как префикс.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String, nullable=False, server_default="", index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    capacity_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    storage_class: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    phase: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    access_modes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    volume_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    disk_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )
    # «Синк видел этот том», в отличие от updated_at = «строка менялась».
    # Различать обязательно: ORM не эмитит UPDATE, когда все поля совпали с
    # прежними, а у PV они не меняются годами — 18.09.2026 из 10 059 строк
    # моложе суток была 61 при живом снимке в 1214. Порог усадки, который
    # считает по «недавно виденным», на таком знаменателе не работает.
    # Обновляется явным batch-UPDATE в конце среза, см. _touch_last_seen.
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "kind", "namespace", "name",
            name="uq_kg_storage_volumes_kind_ns_name",
        ),
        # (kind, namespace) — префикс уникального (kind, namespace, name).
    )


class VolumeEdge(Base):
    """Гетерогенное ребро для storage-графа.

    Не reuse `kg_service_edges`: у src/dst могут быть разные node-типы
    (`Service` ↔ `StorageVolume`), а ServiceEdge FK жёстко на kg_services.id.
    Поэтому tagged ID: `src_kind` + `src_id` (без FK constraint — kind
    указывает на таблицу).

    Поддерживаемые kinds:
        * `uses_volume` — Service → PVC. Источник: scan pod.spec.volumes
          по всем pod'ам, attribuiton к owning Deployment/StatefulSet.
        * `bound_to`    — PVC → PV. Источник: pvc.spec.volumeName.

    `last_seen_at` обновляется на каждом upsert — основа для будущего
    decay-task'а (edges не подтверждённые N дней соответствуют удалённым
    PVC/Pod'ам).
    """
    __tablename__ = "kg_volume_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # `src_kind` ∈ {'service', 'pvc', 'pv'}. Без FK — это namespacing,
    # не reference. Проверка валидности — на app-уровне (populator).
    # Без `index=True` у *_kind: ix_kg_volume_edges_src (src_kind, src_id)
    # и ix_kg_volume_edges_dst (dst_kind, dst_id) покрывают их как префикс.
    src_kind: Mapped[str] = mapped_column(String, nullable=False)
    src_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    dst_kind: Mapped[str] = mapped_column(String, nullable=False)
    dst_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    discovered_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint(
            "src_kind", "src_id", "dst_kind", "dst_id", "kind",
            name="uq_kg_volume_edge_src_dst_kind",
        ),
        Index("ix_kg_volume_edges_src", "src_kind", "src_id"),
        Index("ix_kg_volume_edges_dst", "dst_kind", "dst_id"),
    )


class KGIncident(Base):
    """Инцидент как объект графа: один на сервис, пока он открыт.

    До 06.09.2026 инцидента в графе не было. `kg_alerts.incident_id` во всех
    3766 строках равнялся `fingerprint` — то есть «инцидент» был синонимом
    одного алерта, а таблица `incidents` (артефакт LLM-пайплайна, который в
    проде выключен) стояла пустой. Триаж при этом думает не алертами: у
    сервиса «что-то случилось» — и к этому событию относятся все его алерты
    за окно, деплой перед ним, события подов, аномалии и ошибки в логах.

    Правило простое и детерминированное, без LLM:
      * у пары (namespace, service_name) в каждый момент не больше одного
        ОТКРЫТОГО инцидента — это гарантирует частичный уникальный индекс,
        а не проверка в коде;
      * новый алерт сервиса присоединяется к открытому инциденту, иначе
        заводит новый; алерт, пришедший вскоре после закрытия
        (`REOPEN_WINDOW_MIN`), переоткрывает прежний — флаппинг не должен
        плодить инциденты;
      * инцидент закрывается, когда все его алерты resolved (`kg_alerts.
        resolved_at`), или старится, если давно нет ни алертов, ни резолвов.

    `incident_key` — человекочитаемый стабильный ключ `ns/service@время`; на
    него ссылается `kg_alerts.incident_id`. Целочисленный `id` — для URL и FK.
    `fingerprints`/`alertnames` — JSON-списки: набор небольшой (медиана —
    один алерт), а отдельная таблица связей ради него — лишний join.
    """

    __tablename__ = "kg_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    # NULL = сервис в момент первого алерта в графе не нашёлся (см.
    # Known Unknowns в timeline: без service_id деплои/поды/аномалии не
    # опросить).
    service_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("kg_services.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")   # open | resolved
    severity: Mapped[Optional[str]] = mapped_column(String, nullable=True)                   # максимум по алертам
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)   # fired_at первого алерта
    last_alert_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolve_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # all_alerts_resolved | aged_out
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alertnames: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    fingerprints: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Все алерты инцидента классифицированы обогащением как шум
    # (gen-mismatch при здоровых репликах, meta-агрегаты, rollout-в-процессе).
    # Инцидент остаётся — он честно случился, — но списки по умолчанию его
    # скрывают. Замер 07.09.2026 в первые минуты после релиза: 7 из 13
    # инцидентов — KubeDeploymentGenerationMismatch, 327 из ~390 алертов за
    # неделю. Виды шума — в extras["noise_fingerprints"] по каждому алерту.
    noise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_kg_incidents_ns_service_status", "namespace", "service_name", "status"),
        # Инвариант «один открытый инцидент на сервис» — на уровне БД.
        # Гонка двух реплик API на одном алерт-шторме иначе завела бы два.
        Index(
            "uq_kg_incidents_one_open_per_service",
            "namespace", "service_name",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )


class KGRemediationEvent(Base):
    """Действие ВНЕШНЕГО исполнителя над стендом — то, что сделал не копилот.

    `kg_remediation_decisions` хранит решения собственного executor'а копилота
    и ничего не знает о других роботах. Между тем squad-medic лечит сквады
    каждые 15 минут: 07.09.2026 на ImagePullBackOff squad-39 он применил 13
    бесполезных grant-фиксов и через 14 часов запинговал владельца, а копилот
    в тот же час выложил карточку KubeContainerWaiting по тому же стенду —
    и ни в timeline инцидента, ни в дайджесте действий медика не было.

    Одна строка = один прогон исполнителя по одному стенду: что применил,
    что оставил человеку (`manual`), где сам был слеп (`gaps`), к какому
    инциденту графа это относится (`incident_id`, если на момент прогона по
    namespace стенда был открыт инцидент). Пишется через
    `POST /webhooks/remediation` (HMAC, fail-closed), читается timeline'ом
    инцидента и MCP-тулами. Это шаг «remediation verification» roadmap'а:
    у копилота появляется, ЧТО сверять с состоянием после.
    """

    __tablename__ = "kg_remediation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Кто действовал: `squad-medic`, позже — другие роботы/скиллы.
    #: Без index=True: uq_kg_remediation_events_run (actor, run_id, namespace)
    #: покрывает колонку как префикс.
    actor: Mapped[str] = mapped_column(String, nullable=False)
    #: Идентификатор прогона у исполнителя (имя пода CronJob, iso-время) —
    #: для дедупа повторной отправки.
    run_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Основной namespace (для сквада — `squad-N-shared`), по нему идёт timeline.
    namespace: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: Все namespace стенда, которые исполнитель трогал.
    namespaces: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    squad: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    service_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: fixed | partial | unresolved | failed | noop
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    still_unhealthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    applied: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    manual: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    gaps: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Владельца дёргали в Discord.
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_login: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    incident_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("kg_incidents.id"), nullable=True, index=True)
    extras: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    #: Наблюдения прогона (что робот ВИДЕЛ на стенде: состояния подов, коды
    #: выхода, dirty-миграция, отсутствующие ключи Secret) — без его выводов.
    #: Схема `medic_obs/v1`, пишет POST /webhooks/remediation через
    #: app/context/medic_observations.build_observations; читает сборщик
    #: контекста инцидента из графа как источник `squad-medic`. NULL у событий,
    #: принятых до поля: их наблюдения извлекаются тем же экстрактором на чтении.
    observations: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # Повторная отправка одного прогона (retry исполнителя) — не второе
        # событие. run_id NULL уникальностью не ограничен (SQL-семантика NULL).
        UniqueConstraint("actor", "run_id", "namespace", name="uq_kg_remediation_events_run"),
    )


class K8sNode(Base):
    """Нода кластера: в какой зоне/датацентре она живёт.

    До этой таблицы граф про ноды не знал ничего — ни одного вызова
    `kubectl get nodes` в синках. Вопрос «что ещё стоит в той же зоне» при
    нодовом алерте оставался без ответа, хотя метки
    `topology.kubernetes.io/zone` / `region` на нодах есть.

    Пишет `k8s_nodes_sync.sync_nodes` (строка на имя ноды). Нода, пропавшая
    из API, не удаляется: `deleted_at` ставится, строка остаётся для истории.
    Point-in-time чтение: `first_seen_at <= T` и (`deleted_at` IS NULL или
    `deleted_at > T`). Атрибуты (zone, адреса) — последнее известное
    состояние, а не история.
    """
    __tablename__ = "kg_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    zone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    internal_ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    external_ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: status.addresses целиком: [{"type": "InternalIP", "address": ...}, ...].
    addresses: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    #: Роли из меток node-role.kubernetes.io/<role> (control-plane, ...).
    roles: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    unschedulable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: Только отобранные метки: zone, region и env/kingdom-метки. Все метки
    #: ноды — это сотни ключей служебного шума, и в граф они не нужны.
    labels_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("name", name="uq_kg_nodes_name"),
    )


class TrafficEntrypoint(Base):
    """Точка входа трафика: через какую ноду публичный host попадает в кластер.

    LoadBalancer-сервисы здесь — MetalLB L2 с `externalTrafficPolicy: Local`,
    и публичный IP балансировщика обычно совпадает с адресом самой ноды.
    Значит отказ ноды роняет вход для всех host-ов, что на неё резолвятся,
    — в том числе чужих стендов, чьих подов на этой ноде нет вовсе. Без этой
    таблицы такая связь не видна ни из графа сервисов, ни из алерта.

    Строка на host из Ingress (`spec.rules[].host`, без wildcard). Пишет
    `k8s_nodes_sync.sync_entrypoints`: DNS-резолв host-а (IPv4) сверяется с
    адресами нод (`entry_nodes`) и с IP LoadBalancer-сервисов
    (`lb_services`). Point-in-time — как у `kg_nodes`.
    """
    __tablename__ = "kg_entrypoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    #: Во что host резолвится (IPv4). Пусто — резолв не удался на этом тике.
    resolved_ips: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    #: "namespace/name" LoadBalancer-сервисов с совпавшим IP.
    lb_services: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    #: Имена нод, чей адрес совпал с резолвом host-а.
    entry_nodes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    #: Классы Ingress-ов, которые обслуживают host.
    ingress_classes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("host", name="uq_kg_entrypoints_host"),
    )
