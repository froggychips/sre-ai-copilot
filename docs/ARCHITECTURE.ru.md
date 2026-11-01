# SRE AI Copilot — Архитектура

Состояние на v1.0.33 (2026-11-01).

## 1. Обзор системы

Приложение состоит из: HTTP API (FastAPI), Celery-воркеров и планировщика beat, PostgreSQL (Knowledge Graph и записи инцидентов), Redis (брокер Celery, ключи дедупа, heartbeat-ы beat, снимок self-health), Discord как канала вывода и Kubernetes как наблюдаемой системы (и, через отдельный executor, узко управляемой).

В проде путь алертов **детерминированный**: алерт пишется в KG, обогащается из него SQL-запросами и правилами и рендерится карточкой в Discord. Многогипотезный LLM-пайплайн на фактах (§3, §4) в коде остался, но **ни один маршрут Alertmanager на него не ведёт, и он выключен флагом `LLM_PIPELINE_ENABLED=false`** — `process_incident` возвращает `skipped`, модель не вызывается.

### 1.1 Боевой путь алертов

```
VMAlertmanager (k8s/vmalertmanagerconfig.yaml)
 │
 ├─ prod-*, preprod-* и корневой fallback (алерты без namespace:
 │  ноды, control plane, monitoring/kube-system)
 │    → POST /webhooks/alertmanager/enrich-and-forward      (max_alerts 50)
 │        → запись в KG (kg_alerts, kg_incidents, populate_from_incident)
 │        → детерминированное обогащение (alert_enrichment, без LLM)  §3a
 │        → decide_send (chronic / повтор / подавление на выкате)
 │        → карточка в Discord #infra-error
 │        → wants_bridge? → постановка `remediation_bridge` (Celery)   §2.3
 │
 ├─ squad-*, preupdate-*, любой severity=info
 │    → POST /webhooks/alertmanager/store                   (max_alerts 0)
 │        → только KG — карточки в Discord нет (решение, не пробел)
 │        → wants_bridge? → постановка `remediation_bridge`
 │
 ├─ alertname=Watchdog, severity=none → blackhole
 │
 └─ (маршрута нет) POST /webhooks/alertmanager → полный LLM-пайплайн  §3
                   (LLM_PIPELINE_ENABLED=false)

Внешнее: squad-medic → POST /webhooks/remediation → kg_remediation_events  §2.4
```

Цель алерта (о каком k8s-объекте он) определяется одной цепочкой приоритета меток в `app/services/alert_target.py`: алерт про сам экспортер ложится на экспортер; иначе `deployment`/`statefulset`/`daemonset` → `cronjob`/`job_name` → HPA/PDB → app-алерт → нода → PVC → под → контейнер → `scrape_job`/`job=<ns>/<name>` → `service` → namespace. Исход пишется в `kg_alerts.raw.target_kind` (`service` / `node` / `namespace` / `exporter` / `unresolved`).

Правила карточек, которые стоит знать: повтор горящего алерта, присланный Alertmanager-ом по `repeat_interval`, подавляется (`SUPPRESS_REPEAT`, метрика `alerts_suppressed_total{reason="repeat"}`, флаг `ALERT_SUPPRESS_AM_REPEATS`), а не постится как «RESURFACED»; зелёная карточка «✅ resolved» постится только для critical-эпизода не короче `DISCORD_RESOLVED_NOTICE_MIN_MINUTES` (30). Алерты, горящие непрерывно дольше `STUCK_ALERTS_MIN_DURATION_HOURS` (24), видны в chronic-дайджесте.

## 2. Компоненты времени выполнения

### 2.1 Процессы (namespace `sre-ai`, манифесты в `k8s/`)

| Компонент | Тип | Роль |
|---|---|---|
| `sre-ai-api` | Deployment, 2 реплики | FastAPI на :8000 (вебхуки, `/discord/interactions`, `/kg/*`, `/copilot`, `/healthz`, `/readyz`); Prometheus-метрики на :8001 |
| `copilot-worker` | Deployment, 2 × `--concurrency=2`, `-Q celery` | инцидентный пайплайн, `remediation_bridge`, `remediation_verify`, лёгкие beat-задачи |
| `copilot-worker-sync` | Deployment, 1 × `--concurrency=2`, `-Q sync` | тяжёлые синки KG (с 1.0.28) |
| `copilot-executor` | Deployment, 1 × `--concurrency=1`, `-Q executor` | единственный процесс с правом записи в кластер: ServiceAccount `sre-ai-executor`; исполняет `executor_apply` / `executor_dry_run` |
| `copilot-beat` | Deployment, всегда 1 реплика | Celery beat |
| `postgres` | StatefulSet (`postgres:15-alpine`, local-path PVC) | KG + инциденты (БД `sre_copilot`) |
| `redis` | StatefulSet (local-path PVC) | брокер, дедуп, heartbeat-ы, снимки |
| `postgres-backup` | CronJob, 02:00 UTC | §2.6 |
| `squad-dashboard`, `ci-deadman` | CronJob | витрина сквадов; сторож CI-раннера |

У `postgres` и `redis` PriorityClass `sre-ai-copilot-stateful` (`k8s/priorityclass.yaml`, value 100000, `preemptionPolicy: Never`): тома local-path, переехать на другую ноду они не могут, поэтому выселяются последними.

Деплой: образ собирает CI, раскатка — `kubectl set image` на пять Deployment (плюс env `BUILD_VERSION`, его читает футер карточек); `deploy.sh` применяет `k8s/` только когда меняются манифесты Deployment/NetworkPolicy/RBAC/PriorityClass. StatefulSet-ы `postgres`/`redis` `deploy.sh` не применяет — их меняют `kubectl patch`. Helm-чарт удалён в 1.0.28. Подробности — `docs/RUNBOOK.md`.

### 2.2 Очереди Celery и расписание beat

| Очередь | Кто слушает | Что туда идёт |
|---|---|---|
| `celery` (по умолчанию, `task_default_queue`) | `copilot-worker` | `process_incident`, `generate_reply`, `remediation_bridge`, `remediation_verify`, лёгкие beat-задачи |
| `sync` (`SYNC_QUEUE_NAME`) | `copilot-worker-sync` | `SYNC_QUEUE_TASKS` через `task_routes`: `kg_topology_sync`, `tc_deploys_to_kg`, `kg_storage_sync`, `kg_metrics_sync`, `kg_endpoints_sync`, `kg_signal_aggregates_compute`, `kg_health_retention`, `kg_topology_resources_sync`, `kg_anomaly_detection_task`, `kg_runtime_correlation_sync`, `kg_nats_subjects_sync` |
| `executor` (`EXECUTOR_QUEUE_NAME`) | `copilot-executor` | `executor_apply`, `executor_dry_run` (при `EXECUTOR_DISPATCH=queue`, как в манифестах) |

Каждая beat-задача держит аренду `single_instance`, медленный прогон не накладывается на следующий тик. Длина очередей экспортируется как `celery_queue_length{queue}` (см. `docs/METRICS.ru.md` §7).

Расписание beat (`app/workers/tasks.py`, `beat_schedule`, 36 записей) по группам:

| Группа | Задачи (расписание) |
|---|---|
| Топология и инвентарь | `kg_topology_sync` (каждый час :00), `kg_topology_resources_sync` (15 мин), `kg_ingress_sync` (каждый час :37), `kg_endpoints_sync` (15 мин), `kg_jobs_sync` (15 мин), `kg_storage_sync` (30 мин), `kg_nodes_sync` (30 мин), `kg_namespace_lifecycle` (10 мин), `kg_namespace_owner_sync` (каждый час), `kg_db_edge_rehome` (каждый час), `kg_drift_cleanup` (каждый час :17), `kg_ownership_backfill` (6 ч), `kg_nats_subjects_sync` (6 ч, по умолчанию выключен) |
| События и деплои | `tc_deploys_to_kg` (15 мин), `kg_deploy_watch` (5 мин), `k8s_pod_events_sync` (10 мин), `kg_alerts_resolve_sync` (15 мин), `kg_incidents_lifecycle` (5 мин), `kg_statics_versions_sync` (5 мин) |
| Метрики и сигналы | `kg_metrics_sync` (10 мин), `kg_cluster_health_sync` (5 мин), `kg_ingress_observations_sync` (10 мин), `kg_seq_logs_sync` (10 мин), `kg_anomaly_detection_task` (10 мин), `kg_signal_aggregates_compute` (каждый час :23), `kg_runtime_correlation_sync` (30 мин), `kg_health_recompute` (20 мин), `kg_external_probe` (каждую минуту) |
| Дайджесты и отчёты | `daily_stats_digest` (раз в сутки, `STATS_DIGEST_HOUR_UTC`), `team_daily_digest` (раз в сутки, `TEAM_DIGEST_HOUR_UTC`), `chronic_alerts_digest` (6 ч), `weekly_reconciliation` (пн 06:00 UTC) |
| Надзор и уборка | `kg_self_health_check` (30 мин), `kg_stuck_alerts_check` (каждый час :11), `kg_health_retention` (раз в сутки 03:40 UTC), `discord_dedup_purge` (30 мин) |

Не на beat: `remediation_verify` (`apply_async(countdown)` через +5 и +15 мин после apply, §3f), `remediation_bridge` (ставит API), `process_incident` / `generate_reply`, задачи executor-а.

### 2.3 Детерминированный мост remediation

`app/remediation/` (мост с 1.0.31, исполняется в Celery с 1.0.32). Даёт боевому пути алертов дойти до действия без LLM-пайплайна.

```
API (/enrich-and-forward или /store)
  → wants_bridge(labels): флаг + alertname есть в реестре playbook-ов
  → namespace_allowed: REMEDIATION_BRIDGE_NAMESPACES (префиксы; пусто = все)
  → дедуп постановки: Redis SET NX EX по (alertname, namespace, pod/workload),
    REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS (600) + дедуп внутри батча;
    Redis недоступен → не ставим (fail-closed)
  → apply_async `remediation_bridge` (очередь celery, soft/hard 120/180 с)

Воркер: remediation_bridge
  → факты правил из enrich_alert + состояние пода из K8sFacts
  → match_playbooks (app/remediation/registry/*.yaml)
  → intent собирает сервер из plan.steps playbook-а
  → снимок для матча + bind_server_params → gate (policy)
     approve → снимок цели (uid), server dry-run,
               IncidentRecord(APPROVAL_PENDING) + kg_remediation_decisions,
               карточка с кнопками «Применить / Отклонить» в #infra-error (бот)
     block   → кнопок нет; строка «playbook X: заблокирован policy»
               дописывается PATCH-ем в уже отправленную карточку
               (prod / preprod / system)
  → дедуп предложения: (playbook, uid цели, incarnation) на 6 ч
    (REMEDIATION_BRIDGE_DEDUP_SECONDS)
```

- Код: `bridge.py` (сторона воркера), `bridge_dispatch.py` (дешёвая постановка в API), задача `remediation_bridge` в `app/workers/tasks.py`. В 1.0.31 мост шёл прямо в event loop API и положил API (инцидент 31.10.2026); теперь API только ставит задачу.
- Мост берёт только исполнимые playbook-и, сматченные по alertname (`_candidate_alertnames()` в `bridge.py`). Таких два: `restart_crashloop_deployment` (`KubePodCrashLooping` / `PodCrashLooping`; исключает OOM, свежий деплой и падения по сигналу — exit 132/134/135/136/138/139) и `scale_out_cpu_throttled_deployment` (`CPUThrottlingHigh`; approve только на тирах dev/squad; +1 реплика к живому `spec.replicas`). `cleanup_stale_failed_job` матчится только по `classification`, мост его не берёт.
- Кнопки появляются только при `EXECUTOR_ENABLED` и `EXECUTOR_APPROVAL_ENABLED`. Алерты сквадов по-прежнему в канал не идут; единственное исключение — карточка предложения с policy `approve`.
- Флаги: `REMEDIATION_BRIDGE_ENABLED`, `REMEDIATION_BRIDGE_NAMESPACES`, `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED` (в `app/config.py` все по умолчанию выключены; в проде задаются в Secret `sre-ai-secrets`, а не в манифестах `k8s/`, и включены). Новые флаги включаются сначала на одном namespace под нагрузкой.

Путь клика по кнопке:

```
Discord → Interactions Endpoint приложения бота (ai-reviewer, отдельный
          сервис в ns ai-reviewer-net)
  → пересылает approve:/decline: → POST /discord/interactions (sre-ai-api :8000)
     (NetworkPolicy пускает ai-reviewer-net → API)
  → проверка подписи Ed25519 по DISCORD_PUBLIC_KEY (в копилоте)
  → allowlist DISCORD_APPROVERS_USER_IDS (fail-closed; allowlist ролей в коде
    есть, в проде не настроен)
  → executor_apply (очередь executor, copilot-executor, SA sre-ai-executor)
  → remediation_verify через +5 / +15 мин → итог дописывается в ту же карточку
```

Relay возвращает Discord тело ответа копилота без изменений.

### 2.4 Внешние события лечения (squad-medic)

`POST /webhooks/remediation` принимает отчёты о прогонах внешнего исполнителя — CronJob squad-medic, который лечит стенды сквадов каждые 15 минут. Аутентификация — HMAC-SHA256 над `timestamp.body`, timestamp обязателен, anti-replay (`REMEDIATION_WEBHOOK_SECRET`, fail-closed). Повтор `(actor, run_id, namespace)` идемпотентен. События пишутся в `kg_remediation_events`, привязываются к открытому на тот момент инциденту по любому namespace стенда и попадают в timeline инцидента как `remediation.external`. Поле `fixed` означает «стенд здоров после лечения».

### 2.5 Надзор

- **Self-health** (`kg_self_health_check`, каждые 30 мин): 15 проверок данных KG и источников (§3c), снимок в Redis `stats:self_health:last`, экспортируется метриками API, алерты — `VMRule sre-ai-copilot` (`docs/METRICS.ru.md` §7).
- **Еженедельная сверка** (`weekly_reconciliation`, `app/services/weekly_reconciliation.py`, пн 06:00 UTC, #stats, `WEEKLY_RECONCILIATION_ENABLED`, по умолчанию вкл.): сверяет копилот с внешним миром — покрытие алертов (`ALERTS` в VictoriaMetrics против `kg_alerts` за 7 дней), рост таблиц (топ-5 по размеру против прошлой недели), возраст бэкапа (`kube_cronjob_status_last_successful_time`), доля алертов без атрибуции, подтверждённые починки (verified-попытки executor-а + `fixed` у медика). Недоступный источник — «не измерено», а не ноль.
- **Дайджесты**: `daily_stats_digest`, `team_daily_digest`, `chronic_alerts_digest` (раз в 6 ч, `CHRONIC_DIGEST_ENABLED`; включает алерты, горящие непрерывно дольше 24 ч).

### 2.6 Жизненный цикл данных

- **Ретеншен** (`app/knowledge_graph/health_retention.py`, задача `kg_health_retention`, раз в сутки 03:40 UTC, удаление батчами). Сэмплы (`RETENTION_TARGETS`, пол `MIN_RETENTION_DAYS`=7): `kg_service_health` (`KG_HEALTH_RETENTION_DAYS`, 30), `kg_anomaly_observations`, `kg_signal_aggregates`, `kg_ingress_observations`, `kg_log_observations` — 30 дней; `kg_cluster_observations` — 90. События (`EVENT_RETENTION_TARGETS`, пол `MIN_EVENT_RETENTION_DAYS`=90): `kg_deployments` 180 дней по `started_at`, `kg_alerts` 180 и `kg_incidents` 365 по `resolved_at` (открытые не удаляются никогда), `kg_pod_events` 90 по `last_seen`, `kg_remediation_events` 365.
- **Объём записи**: upsert-ы KG не переписывают неизменившиеся строки (`SERVICE_TOUCH_INTERVAL` 30 мин, `EDGE_TOUCH_INTERVAL` 3 ч в `populator.py`); `k8s_endpoints_sync` пишет metadata только при смене `endpoints_ready`.
- **Бэкап** (`k8s/postgres-backup.yaml`, CronJob 02:00 UTC): `pg_dump` (custom, gzip) на PVC `postgres-backup-data` на другой ноде, чем база, хранятся последние `BACKUP_KEEP` (7) дампов; затем `aws s3 cp` в бакет из Secret `postgres-backup-s3` (без секрета выгрузка пропускается). Алерты: `CopilotPostgresBackupFailed` / `CopilotPostgresBackupMissing`.

### 2.7 Логирование

`configure_logging()` из `app/observability/logging_setup.py` вызывается в `app/main.py` (API) и `app/workers/tasks.py` (все процессы Celery: worker, worker-sync, executor, beat). Уровень — `LOG_LEVEL` (по умолчанию INFO); `httpx`/`httpcore` подняты до WARNING, потому что пишут полный URL, включая токены в пути вебхуков. Формат — консольный рендерер structlog по умолчанию, не JSON. Celery не перехватывает stdout (`worker_redirect_stdouts=False`); строки access-лога uvicorn по `/healthz` и `/readyz` со статусом < 500 отбрасывает `app/observability/access_log.py`. Audit-лог (`app/services/audit_logger.py`) пишет JSON-строки в stdout.

### 2.8 Модули

- **API app (`app.main`)**: маршрутизация, auth dependency, middleware метрик, эндпоинты health/readiness.
- **Вебхуки (`app.api.webhooks`)**: четыре endpoint-а из §1.1; `/webhooks/alertmanager` ставит `process_incident`, который гейтнут `LLM_PIPELINE_ENABLED`.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: фоновый разговорный анализ с петлёй по порогу конфиденса (`generate_reply`, то же Celery-приложение).
- **DiagnosticsEngine (`app.diagnostics.engine`)**: правила → типизированный `FactStore`. Используется и LLM-пайплайном, и детерминированным путём.
- **FactStore (`app.diagnostics.facts`)**: канонический источник правды об инциденте; поддерживает детекцию конфликтов и сериализацию в промпт.
- **MultiHypothesisAgent (`app.agents.multi_hypothesis`)**: fan-out по 4 перспективам (app/infra/deps/runtime) с фильтром по предусловиям. *Только LLM-пайплайн.*
- **FactCriticAgent (`app.agents.fact_critic`)**: adversarial grounding — отсекает гипотезы, противоречащие наблюдаемым фактам. *Только LLM-пайплайн.*
- **FixAgent (`app.agents.fix`)**: генерирует структурированный `ExecutionIntent`; учитывает рецидивы и Jira-контекст. *Только LLM-пайплайн.*
- **SimilarIncidentEngine (`app.core.intelligence.similar_incidents`)**: KG-детекция рецидивов (окно 7 дней).
- **JiraClient (`app.context.jira_client`)**: обогащение через Atlassian REST API.
- **Knowledge Graph (`app.knowledge_graph.*`)**: автоматически наполняемый направленный граф в Postgres. Ядро: `kg_services`, `kg_service_edges`, `kg_deployments`, `kg_alerts`, `kg_incidents`, `kg_pod_events`, `kg_namespaces`; storage — `kg_storage_volumes` + `kg_volume_edges`; jobs — `kg_k8s_jobs` + `kg_k8s_job_runs`; ноды и точки входа трафика — `kg_nodes` + `kg_entrypoints`; remediation — `kg_remediation_decisions`, `kg_remediation_attempts`, `kg_remediation_events`; временные ряды (§3b). Schema/quality contract — `app/knowledge_graph/contract.py` (`KG_SCHEMA_VERSION` = 2.9, история — `docs/KG_SCHEMA_CONTRACT.md`). У узла есть `node_kind` (`service` / `workload` / `ingress`): k8s Service и его backing Deployment — **разные узлы**, поэтому ребро `serves_traffic` между ними вообще может существовать.
- **Alert enrichment (`app.services.alert_enrichment`)**: детерминированное KG-обогащение для `/webhooks/alertmanager/enrich-and-forward` — без LLM, собирает `EnrichedContext` (recent_deploys, upstream_alerts, outgoing_deps, pod_events, jira_issues, primary_hypothesis, why_this_matters).
- **Remediation (`app.remediation`)**: реестр playbook-ов, matcher, привязка, policy-gate, попытки, верификация, детерминированный мост (§2.3).
- **Data layer (`app.database`, `app.repository`, `app.models`)**: SQLAlchemy-модели и CRUD-операции.
- **Integration layer (`app.services.mcp_client`)**: MCP-клиент для k8s, TeamCity и других внешних инструментов.
- **Safety services**: менеджер аппрувов, K8s guard, execution DSL, gate executor-а (`app/remediation/registry/_executor_apply_gate.yaml`).

## 3. Поток данных (LLM-пайплайн инцидента — в проде не маршрутизируется)

> **Статус:** ни один receiver Alertmanager не ведёт на `/webhooks/alertmanager`, а `process_incident` возвращает `skipped`, пока `LLM_PIPELINE_ENABLED=false` (дефолт и значение в проде). Для включения нужен ещё конечный `LLM_DAILY_BUDGET_USD` (валидатор конфига). Ниже — путь в коде, оставлен для справки.

```
AlertManager webhook
  → POST /webhooks/alertmanager
  → для каждого алерта: IncidentRecord(status=PENDING)
  → Celery task: process_incident

  Стадия 1: Context Builder
    → k8s_pod_state (live метаданные пода)
    → vm_metrics (память/CPU из VictoriaMetrics)
    → teamcity_context (MCP — последние деплои)

  Стадия 2: DiagnosticsEngine
    → правила: OOMKilledRule, ProcessCrashRule, CrashLoopRule, …
    → выдаёт: FactStore{oom_killed, process_crash, crashloop, …}
    → детекция конфликтов: MUTUALLY_EXCLUSIVE_PAIRS → cap confidence

  Стадия 3: MultiHypothesisAgent
    → PERSPECTIVE_PRECONDITIONS фильтр (runtime требует process_crash)
    → параллельный LLM fan-out по активным перспективам
    → FactCriticAgent grounding → выжившие

  Стадия 4: Jira enrichment (best-effort)
    → JiraClient.search_by_service(service, namespace)
    → build_jira_context() → {open, resolved, has_open}

  Стадия 5: FixAgent
    → recurrence-aware (_RECURRENCE_PREFIX при is_recurrence)
    → Jira-enriched (_build_jira_prefix при jira_context)
    → генерирует ExecutionIntent JSON

  Стадия 6: RiskAgent → Discord approval
  Стадия 7: IncidentRecord(status=COMPLETED, analysis=…)
  Стадия 8: обновление KG (_is_quality_cause фильтр)
```

## 3a. KG-only Enrichment Flow (без LLM)

Боевой путь (§1.1): детерминированное обогащение алертов без LLM, единственный канал вывода алертов в Discord. `/webhooks/alertmanager/store` делит с ним запись в KG (и постановку моста remediation), но карточку не шлёт.

```
AlertManager webhook
  → POST /webhooks/alertmanager/enrich-and-forward
  → store incident → KG (populate_from_incident)
  → группировка алертов по (alertname, severity)
  → для каждой группы:
     → alert_enrichment.enrich_alert(db, incident)
        → recent_deploys_for(...)         # с adaptive effective_at
        → nearby_alerts(...)              # корреляция upstream-alerts
        → incidents_on(...)               # recurrence window
        → _downstream_count_by_kind(...)  # inbound callers
        → upstream_of(..., fresh_only_days=30)  # outgoing deps с confidence
        → recent_pod_events_for(...)      # k8s diagnostic signal
        → JiraClient.search_by_service_sync(...)  # ticket linkback
        → PodEventsRule + RecentDeployRule + UpstreamDegradedRule
        → primary_hypothesis() + why_this_matters()
        → noise-флаги: rollout_noise / meta_noise / gen_mismatch_noise (ставятся на EnrichedContext, см. §3a-noise)
     → decide_send() (chronic suppress / rollout-silent)
     → DiscordService.send_enriched_alert(contexts, env, resurfaced)
        → severity-aware embed (muted-render — grey + 🔇 — если выставлен noise-флаг)
        → confidence badges (●●●/●●○/●○○) с provenance
        → кликабельные TC build links + deployer name
     → wants_bridge? → постановка remediation_bridge (§2.3)
```

Latency budget: <500ms p95 synchronous в HTTP handler. 0 LLM-токенов. Стоимость на alert: 0.

### Подавление шума alert-quality — три разных механизма (v0.14.0)

Пайплайн подавляет шум на **трёх** разных уровнях. Их нельзя путать — держим таксономию явно:

| # | Механизм | Где | Что с карточкой | Severity | Примеры |
|---|---|---|---|---|---|
| 1 | **DROP на входе** | webhook handler, **до** KG-store и enrichment | карточка вообще не появляется | n/a (отфильтрована) | `ALERT_SUPPRESS_NAMES` (default `Watchdog`, `InfoInhibitor`, `KubeAPIServerSlo…`), расширяется через `ALERT_SUPPRESS_NAMES_EXTRA` |
| 2 | **MUTE на render-е** | render / `send_enriched_alert` | видна, но тихо — grey + 🔇, без 🚨, без @mention; severity **не** демотится, карточка **не** дропается | сохраняется | `rollout_noise`, `meta_noise`, `gen_mismatch_noise` (ниже) |
| 3 | **chronic suppress** | `decide_send()` | сворачивается в периодический chronic-дайджест вместо живого пинга | сохраняется | повторно-флапающие chronic-алёрты (см. `chronic_alerts_digest`) |

Три класса **MUTE** (механизм #2) выставляются булевыми флагами на `EnrichedContext` в `enrich_alert` (шаг 9 enrichment-флоу выше) и читаются muted-render-путём в `app/services/discord/service.py` (color / icon / mention):

- **`rollout_noise`** (`ROLLOUT_SUPPRESS_ENABLED`, окно `ROLLOUT_SUPPRESS_WINDOW_MINUTES`=15) — приглушает алёрты, сработавшие в окне активного деплоя (есть `kg_deployments` для резолвенного сервиса за последние N минут). Детектор `_detect_rollout_noise(incident, recent_deploys)`.
- **`meta_noise`** (`META_NOISE_ENABLED`, default true) — **ВСЕГДА-шумные** мета-агрегаты: семейство `*NewCriticalAlerts` (Prod/Preprod/Squad/…) плюс производные control-plane scrape-gap `etcdInsufficientMembers` / `ScrapePoolHasNoTargets` / `RecordingRulesNoData`. Приглушать безопасно, потому что каждый реальный критикал и так приходит отдельной громкой карточкой со своим сервисом/деплоем/KG — агрегат-счётчик лишь повторяет его. Детектор `_detect_meta_noise`, тег рендера **🔇 META-AGGREGATE**. Прецедент: ProdNewCriticalAlerts 2026-06-16 (PR #154).
- **`gen_mismatch_noise`** (`GEN_MISMATCH_NOISE_ENABLED`, default true) — **УСЛОВНЫЙ** churn `KubeDeploymentGenerationMismatch`. `metadata.generation != observedGeneration` штатно флапает, когда внешний контроллер (Rancher / cattle-cluster-agent дописывает аннотацию `publicEndpoints`) бьёт `generation`, а deployment-контроллер на миг отстаёт `observedGeneration` — накат давно сошёлся. Но тот же alertname сигналит и реальный зависший накат. Различитель — **здоровье реплик** `ctx.replicas_ready_desired`: приглушаем **ТОЛЬКО** при `ready==desired (≥1)`; при `ready<desired` / `?/N` / `None` / `0/0` алёрт остаётся **fail-safe LOUD** (реальный зависший накат звенит в любом ns, включая prod). Health-gating > namespace-скоуп. Детектор `_detect_gen_mismatch_noise(incident, replicas_ready_desired)`, тег рендера **🔇 GENERATION-CHURN**. Прецедент: prod-kingdom7 / town-service 2026-06-23 (PR #160). `rollout_noise` имеет приоритет — `gen_mismatch_noise` вычисляется только когда `rollout_noise` false.

Тесты: `tests/test_meta_noise_suppression.py` (12), `tests/test_gen_mismatch_noise.py` (14).

## 3b. Active Observability Layer (Wave 1–5)

Поверх дискретных таблиц событий (deployments / alerts / pod_events) KG также материализует непрерывный time-series слой по каждому сервису — он питает дайджест, anomaly-детектор, deploy correlator и Discord pipeline.

### Time-series materialization (Wave 1)

| Таблица | Источник | Гранулярность |
|---|---|---|
| `kg_service_health` | VictoriaMetrics PromQL через `metrics_sync.py` | 10 мин, на сервис |
| `kg_cluster_observations` | k8s node API через `cluster_health_sync.py` | 5 мин, на ноду |
| `kg_ingress_observations` | nginx-ingress PromQL через `ingress_observations_sync.py` | ~10 мин, на host/path |
| `kg_signal_aggregates` | 24-h roll-up через `signal_aggregates.py` | hourly refresh (:23), на сервис |

Каждая запись из PromQL оборачивается в Postgres `SAVEPOINT`; `IntegrityError` на UNIQUE-ключе (например, дубль `(service_id, ts)`) откатывает savepoint и продолжает цикл. Поэтому sync естественно идемпотентен на пересекающихся окнах.

Wave 5 PromQL gotcha (фиксируем для будущих авторов): `mem_pct` нужно считать как `avg(rate(container_memory_working_set_bytes) / on(pod) kube_pod_container_resource_limits)` — сначала делим, потом агрегируем. Форма «aggregate-then-divide» несколько дней молча возвращала нули, потому что many-to-one join схлопывал результат до деления. Canary `kg_self_health` (ниже) добавлен специально, чтобы такие тихие деградации детектились автоматически.

Coverage-апдейт (2026-06-10): nginx-ingress метрики включены на обоих контроллерах кластера WO (`--enable-metrics=true` на shared `ingress-nginx-controller`, 16 нод, и `ingress-prod-controller`, 5 prod-нод). Per-host лейблы сохранены — `metrics-per-host` по умолчанию true и выключать его НЕЛЬЗЯ: sync копилота фильтрует по host. Скрейп идёт через VMPodScrape `ingress-nginx-shared` + `ingress-prod` в ns `cattle-system` с `honorLabels: true` (обязателен — иначе лейбл `namespace` перетирается в `exported_namespace`), подхватывается VMAgent-ом в ns `monitoring` (`selectAllByDefault`). В результате `kg_ingress_observations` наполняется per-host/path p95/p99/rps/4xx/5xx; первые 50 минут дали 156 рядов по 23 хостам, покрыты все контуры (prod/squad/preprod/preupdate/infra), 100% рядов слинкованы с `kg_services`.

Оставшееся честное ограничение: per-service `http_5xx_rate` и `p95_latency_ms` в `kg_service_health` ПО-ПРЕЖНЕМУ всегда 0 — `/metrics` ASP.NET-сервисов (Kestrel) закрыт JWT-middleware (401). Фикс — бэкенд-тикет WO-12483 (рекомендация: отдельный management-порт без auth); после его раскатки добавится VMServiceScrape на app-ns и поля оживут. До этого `health_score` остаётся инфра-прокси (alerts + pod_events + aggregates), а `log_error_rate` — лог-прокси, не HTTP 5xx. Pipeline деградирует gracefully: anomaly detection на плоском нуле просто не выдаёт observations.

### Anomaly detection (Wave 2 + Wave 6 update)

`app/knowledge_graph/anomaly_detection.py` сканирует материализованный time-series и пишет `kg_anomaly_observations`.

- **Robust-z статистика.** `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Median + MAD вместо mean + stddev — одиночный выброс в baseline не отравляет порог.
- **Seasonal baseline.** При ≥50 исторических точках baseline стратифицируется по hour-of-day, чтобы поглотить дневной паттерн. `extras.method = robust_z_seasonal`. Ниже порога — flat baseline, `robust_z_flat`.
- **Configurable thresholds** через `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) и `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`).
- **Volume guard.** Не более 3 observations на (service, metric) в час — защита от flood'а при затяжной аномалии.
- **Baseline window:** rolling 7 дней.

### Deploy ↔ Incident correlator (Wave 2 + Wave 6 update)

`app/rca/deploy_correlator.py` вызывается inline из `IncidentPipeline._enrich_deploy_correlation` для каждого инцидента, дошедшего до enrichment. Это часть LLM-пайплайна (`app/workers/pipeline.py`), поэтому в проде при `LLM_PIPELINE_ENABLED=false` он не выполняется.

- **Окно:** `[incident − 2h, incident]`. Все деплои сервиса внутри окна — кандидаты.
- **Confidence formula.** Multi-factor score, каждый компонент в `[0, 1]`:
  - `N_spikes`: число anomaly observations после деплоя
  - `max_zscore`: пиковый robust-z в окне
  - `time_proximity`: близость деплоя к началу инцидента
  - `deploy_status_factor`: FAILED-деплои весят больше, чем SUCCESS
  - `flat_baseline_penalty`: если baseline был всё нули — confidence сжимается (всплеск на мёртвой метрике — не сигнал)
- **Verdict tiers:**
  - `likely` при confidence ≥ 0.7 — отрисовывается в Discord как блок "Suspect deploy"
  - `suspect` при 0.4 ≤ conf < 0.7 — отрисовывается с более слабой формулировкой
  - `weak` при 0.2 ≤ conf < 0.4 — остаётся в analysis JSON, не в embed
  - `unlikely` при conf < 0.2 — пишется для аудита, не показывается
- Доступно downstream через `analysis["deploy_correlator"]` — для Discord-рендера и любых будущих RCA-потребителей.

### Seq logs integration (Wave 2)

`app/knowledge_graph/seq_logs_sync.py` опрашивает Seq REST API раз в 10 минут и пишет `kg_log_observations`, по одной строке на `(service, level)` за 10-минутное окно.

- Match-алгоритм: Seq-property `Application`/`Service` → `kg_services.name`, fallback по namespace prefix.
- Используется Discord-пайплайном для блока "log error rate", когда аномалии совпадают по времени.
- Известное ограничение: сейчас pipeline пишет всё как `Information`, потому что доступные реалмы отдают один совмещённый поток. Чтобы заполнять `Error`/`Warning` независимо, нужны per-realm Seq или access-log routing. Схема и downstream-потребители уже level-aware; деградирует только источник.

### Daily team digest (Wave 2)

`app/services/team_digest.py` (задача `team_daily_digest`) запускается ежедневно в `TEAM_DIGEST_HOUR_UTC` и собирает per-team Discord embed:

- Fragile top-5 сервисов по `health_score` (самые низкие)
- Deploy success-rate за последние 24 ч
- Open alerts breakdown (firing / chronic-suppressed / resolved)
- Top alertnames за 24 ч
- SLO burn rate где доступно

### Discord pipeline overhaul (Wave 3)

Discord-рендерер (`app/services/discord_service.py`) переписан из "send and forget" в stateful publisher:

- **Dedup window**: 30 мин. Повтор того же логического алерта PATCH'ит существующее сообщение через `webhook?wait=true`, а не репостит.
- **Severity routing**: `critical` / `warning` → канал; `info` / `none` → silent (только аудит). Настраивается по среде.
- **Per-team channel routing** через `DISCORD_TEAM_CHANNEL_MAP` (JSON: `{team_owner: webhook_url}`); fallback на дефолтный webhook.
- **Suspect deploy block** отрисовывается при `deploy_correlator.verdict ∈ {likely, suspect}`.
- **Log error rate block** — когда `kg_log_observations` показывает аномалию в том же окне.
- **Anomaly block** — из `kg_anomaly_observations`.
- **Recurrence block** — "×N in 24h · M in 7d" — на основе истории KG.
- **Linked alerts aggregation** — алерты upstream/downstream сервисов, тоже firing, сворачиваются в один embed.
- **Persistent approval** — ORM-таблица `ActionApproval` с UNIQUE `(incident_id, intent_signature)`; Approve / Decline кнопки уходят в Discord interactions endpoint, который записывает решение.

## 3c. Security & Operational Hardening (Wave 6)

### PII redaction

`app/services/pii_redaction.py` — regex-based редактор с паттернами:

- email
- IPv4 / IPv6 литералы
- JWT (3-сегмент base64)
- `Bearer …` заголовки авторизации
- UUID
- длинные hex-строки (вероятные fingerprint'ы / ключи)
- `key=value` где key матчит `password|token|secret|api_key`

Применяется на двух уровнях:

1. **Write-time** в `seq_logs_sync.py` — log lines редактируются до записи в `kg_log_observations.message_sample`, с truncation до 500 символов. БД остаётся чистой.
2. **Defense-in-depth** на render-time в `discord_service.py` — каждая строка, идущая в Discord webhook, прогоняется через редактор повторно, на случай если новый источник данных обойдёт write-time scrubbing.

Редактор идемпотентен: повторный прогон даёт тот же результат.

### Approve / Decline authorization

Discord interactions endpoint (`app/api/discord_interactions.py`) теперь гейтит клики Approve / Decline:

- `DISCORD_APPROVERS_USER_IDS` — CSV-список Discord user ID (в проде настроен только он)
- `DISCORD_APPROVERS_ROLE_IDS` — CSV-список Discord role ID (member должен иметь хотя бы одну); поддерживается, в проде не используется
- `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` — квота на пользователя, default 5

Семантика:

- **Fail-closed** когда оба списка пустые — кнопка отказывает, audit `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.
- Неавторизованный клик получает ephemeral deny и audit `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
- Rate-limit in-memory. Authz-fail НЕ потребляет квоту — отказанного пользователя нельзя использовать для исчерпания лимита легитимного approver'а.

### KG self-health canary

`app/knowledge_graph/self_health.py` — beat-задача "monitoring of the monitoring", добавлена после Wave 5 mem_pct silent-failure, чтобы такой класс регрессий ловился автоматом.

Запускается раз в 30 минут (`kg_self_health_check`). Пятнадцать проверок (`_ALL_CHECKS`), каждая возвращает `ok` / `warn` / `fail`; источник, выключенный конфигурацией, отчитывается `disabled` и общий статус не поднимает. Исходные шесть:

1. **`materialization_zero_rate`** — % строк в `kg_service_health` где метрика = 0/NULL за 24 ч. Allowlist для known-zero метрик (`http_5xx_rate`, `p95_latency_ms`, пока app `/metrics` за JWT — WO-12483).
2. **`sync_lag`** — `max(ts)` на beat-задачу vs ожидаемый интервал; >2× → warn, >5× → fail.
3. **`anomaly_signal_health`** — count `kg_anomaly_observations` за 24 ч. 0 → warn (flat baseline или детектор сломан). >500 → warn (порог слишком слабый, overload).
4. **`alerts_resolve_freshness`** — открытые `kg_alerts`, у которых `coalesce(last_notified_at, fired_at)` старше 7 дней. >20 → warn. Долгие алерты, которые AM всё ещё присылает, перечислены отдельно в `long_firing_alerts`.
5. **`pod_events_link_rate`** — % `kg_pod_events` за 24 ч с `service_id NOT NULL`. <80% warn, <50% fail (StS resolver regression).
6. **`edges_freshness`** — % `kg_service_edges` с `last_seen_at < 24h` или NULL. >30% stale → warn (kg_topology_sync regression).

Добавлены позже: `digest_delivery`, `edge_kind_freshness`, `node_freshness`, `source_coverage` (`SourceStatus` по источникам, включая `cleanup_blocked`), `silent_gaps`, `deploy_stream_ingestion`, `graph_integrity` (фантом-дубли db-узлов, self-loop `serves_traffic`, висячие рёбра), `orphan_namespaces`, `schema_version`.

Вывод: строка в audit-log за прогон; снимок пишется в Redis `stats:self_health:last` и экспортируется метриками (§3f, `docs/METRICS.ru.md` §7); на любой `fail`/`warn` — single Discord embed в `DISCORD_WEBHOOK_SELF_HEALTH_URL` (отделено от `#infra-error`, чтобы не топить операционные алерты). 6-часовое dedup-окно не даёт одному и тому же failing-canary заспамить канал.

## 3d. Topology Expansion (Wave 7)

Wave 7 расширяет источники топологии KG с двух (env-var heuristic +
Ingress-host externalisation) до пяти, плюс runtime confirmation channel.
Цель — дать confidence-фреймворку (`kg_service_edges.extras.discovery_sources`)
независимые tier-1 источники, чтобы провенанс edges не зависел от
единственного heuristic-парсинга env-переменных.

### Wave 7-X: declarative Service + Ingress parser

`app/knowledge_graph/k8s_topology_resources_sync.py` каждые 15 минут читает
`kubectl get services/ingresses -A -o json` и строит:

- **`serves_traffic`** edge: Service → backing Deployment, по selector-match
  на pod template labels. Declarative замена runtime Endpoint resolution —
  не зависит от живых pods, поэтому работает и для свёрнутых деплоев.
- **`routes_to`** edge: Ingress (как ресурс) → backend Service. Параллельный
  slice к существующему `k8s_ingress_sync.py`, который строит
  `ingress:<host>` → backend как `calls` (host-уровень).

RBAC: cluster-role требует `services` + `ingresses` на `get`/`list`/`watch`
(см. `k8s/base/rbac.yaml`). Включён по умолчанию.

### Wave 7-Y: PodEvent runtime correlation

`app/knowledge_graph/runtime_correlation.py` каждые 30 минут ищет пары
сервисов, у которых warning-события (BackOff/Unhealthy/OOMKilled/
FailedScheduling/CrashLoopBackOff/FailedMount/ImagePullBackOff) сваливаются
в одном окне (default 15 мин) N+ раз за окно (default 7 дней).

**Что делает.** Подтверждает уже существующие edges новым
`discovery_source = "kg_sync/runtime_corr"` (tier-1 precedence 0.95).
Дешёвый OTEL-substitute: вместо distributed tracing смотрим на
наблюдаемую корреляцию failure-сигналов.

**Что НЕ делает.** Новые edges не создаёт. Симметричный co-fail сигнал
не определяет направление зависимости. Direction discovery остаётся за
declarative-источниками.

Feature flag: `RUNTIME_CORRELATION_ENABLED=true` (включён по умолчанию).

### Wave 7-Z: NATS subjects parser

`app/knowledge_graph/nats_subjects_sync.py` каждые 6 часов клонирует
(shallow + sparse-checkout) WO monorepo, regex-парсит C# исходники на
NATS-consumers и publish call-sites. Subject регистрируется как
synthetic-Service в namespace `nats-subjects`,
`name = subject:<value>`. Edge `uses_nats` с `extras.direction ∈ {pub, sub}`,
`weight = count(call-sites)`.

Feature flag: `NATS_SUBJECTS_PARSER_ENABLED=false` (выключен по умолчанию —
требует ssh-доступ к gitlab-monorepo и каталог `WO_MONOREPO_PATH`).

### Edge kinds (полный реестр после Wave 7)

| Edge kind | Producer | Direction semantics |
|---|---|---|
| `calls` | `kg_sync` (env-var URL), `k8s_ingress_sync` (host) | A делает HTTP-вызов к B |
| `uses_nats` (cluster-level) | `kg_sync._extract_nats_clusters` | Service использует NATS-кластер (shared/kingdom) |
| `uses_nats` (subject-level) | `nats_subjects_sync` | Service публикует/читает subject; `extras.direction ∈ {pub, sub}` |
| `uses_db` | `kg_sync` (secretKeyRef heuristic) | Service использует БД (без чтения значений secret) |
| `serves_traffic` (Wave 7-X) | `k8s_topology_resources_sync` | Service → backing Deployment (по selector) |
| `routes_to` (Wave 7-X) | `k8s_topology_resources_sync` | Ingress (ресурс) → backend Service |
| `runs_as_job` (Wave 8-A) | `k8s_jobs_sync` | CronJob/Job → owner Service (через `K8sJob.owner_service_id`, metadata-only) |
| `uses_volume` (Wave 8-B) | `k8s_storage_sync` | Service → PVC (в `kg_volume_edges`) |
| `bound_to` (Wave 8-B) | `k8s_storage_sync` | PVC → PV (в `kg_volume_edges`) |

## 3e. KG Metadata + UX Polish (Wave 8)

Wave 8 — пакет из 17 PR, который доводит KG-метаданные и Discord UX до
состояния «можно строить Phase A remediation поверх». Контракт становится
формальным, добавляются два больших coverage-блока (Jobs/Storage), owner
inference перестаёт быть prefix-only, появляется `quality_report` для
baseline-снимков перед remediation-волной.

### Wave 8-A: k8s Jobs/CronJobs coverage (PR #82)

`app/knowledge_graph/k8s_jobs_sync.py` каждые 15 минут читает
`kubectl get jobs,cronjobs -A -o json` и upsert'ит в новую таблицу
`kg_k8s_jobs` (отдельная от `kg_services` — Job/CronJob не «service»).

- **Закрывает blind spot**: alembic migration jobs, backup CronJob-ы
  (etcd snapshot, push-s3), ad-hoc cleanup — невидимы для KG до Wave 8.
- **Поля Jobs**: `succeeded_count` / `failed_count` / `active_count` /
  `completion_time` / `last_pod_exit_code`.
- **Поля CronJobs**: `schedule` / `last_schedule_time` /
  `last_successful_time` / `suspended`.
- **Semantic edge `runs_as_job`**: CronJob → owner Service через
  `K8sJob.owner_service_id` metadata-column (не отдельный edge-row).
  Match по label `app.kubernetes.io/part-of` / `app` совпадающий с
  `kg_services.name` в том же namespace.

### Wave 8-B: PVC/PV storage coverage (PR #84)

`app/knowledge_graph/k8s_storage_sync.py` каждые 30 минут.

- **Закрывает blind spot**: ClickHouse / Postgres «упал» = в 95% случаев
  диск кончился; до Wave 8 KG не имел storage-слоя.
- **Новые таблицы**: `kg_storage_volumes` (PVC/PV; `kind ∈ {pvc, pv}`;
  PV cluster-scoped с `namespace=''`) + `kg_volume_edges` (heterogeneous
  edges с tagged `src_kind`/`dst_kind`, без FK constraint).
- **Атрибуты**: storage_class, access_modes, phase, capacity_bytes.
- **Edges**: `uses_volume` (Service → PVC, через scan
  `pod.spec.volumes[]`), `bound_to` (PVC → PV через `pvc.spec.volumeName`).
- **disk_pct enrichment** опционален через `STORAGE_METRICS_ENABLED=false`
  (default OFF — kubelet_volume_stats_* scrape config может быть не настроен).

### Wave 8-C: multi-signal owner inference (PR #85)

`app/services/ownership_suggester.py` + `app/services/owner_aliases.py`.
Старая prefix-only логика расширена до взвешенного fusion-а трёх сигналов
+ manual override:

- **A. Prefix** (weight 0.4) — regex по ns-имени.
- **B. Deploy history** (weight 0.4) — most-frequent `triggered_by` из
  `kg_deployments` за 30 дней; username транслируется через
  `owner_aliases.resolve_username`.
- **C. Labels** (weight 0.2) — k8s labels из `kg_services.metadata_json`.
- **Manual override** через `OWNERSHIP_MANIFEST_PATH=ownership.yaml`
  (match по glob → confidence=1.0, эвристики игнорируются).

### Wave 8-D: stale_class column (PR #86)

`kg_services.stale_class` + `app/knowledge_graph/stale_classifier.py`:

- `active` — deploy за последние `ACTIVE_WINDOW_DAYS` (default 30d).
- `expected_stale` — давно не катился, но это норма (backup/cron/system
  ns, infra/platform owner).
- `suspicious_stale` — нет deploys 30d, не expected_stale.

`kg_sync.sync_namespace` переписывает column идемпотентно; stats_digest
читает column как primary, fallback на legacy `_classify_stale`.

### Wave 8-E: schema/quality contract v2.2 (PRs #83 + #89)

`app/knowledge_graph/contract.py` — единственный источник истины:

- `KG_SCHEMA_VERSION = "2.9"` (Wave 7 → 2.1; PR #82/#84/#86 → 2.2; orphan на
  app-scope → 2.3; `node_kind` → 2.4; orphan перестал считать `serves_traffic`
  связностью → 2.5; `owner_source` → 2.6; `stale_class = gone` → 2.8;
  владелец namespace `kg_namespaces.owner_*` → 2.9). История и мотивация —
  `docs/KG_SCHEMA_CONTRACT.md` §1.
- `EDGE_KINDS` — реестр всех edge kinds (semantic / src_kinds / dst_kinds /
  source / status / table); поле `table` различает где edge живёт
  (`kg_service_edges` / `kg_volume_edges` / `fk_only` / `metadata_only`).
- `OWNER_SOURCES`, `STALE_CLASS_VALUES`, `SYNTHETIC_KINDS`.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика drift-а.
- **Gate #22** (`tests/test_contract_drift.py`) — CI-блокирующий тест на
  расхождение кода и контракта.

Человекочитаемая версия — `docs/KG_SCHEMA_CONTRACT.md`.

### Wave 8-F: Discord UX polish (PRs #76, #77, #79, #81, #90)

- **#76**: blast radius (X) / NATS impact (Z) / pod trail (Y) теперь
  рендерятся в enriched embed как отдельные поля.
- **#77**: PATCH-dedup для `send_enriched_alert` — 30-минутное content-key
  окно.
- **#79**: human-time, pod name, ready/desired replicas, kubelet reason.
  Новый модуль `app/utils/time_human.py`.
- **#81**: stats digest UX — series→series collapse, unowned action
  block, `trend Δ24h`, секции `chronic`/`resurfaced`, stale pill,
  `fragile → blast-radius` rename.
- **#90**: cascade deploys aggregation, new-baseline placeholder.

### Wave 8-G: quality_report + snapshot fixtures (PRs #87, #88)

- **`app/scripts/quality_report.py`** — идемпотентный read-only CLI:
  5 групп метрик KG. Markdown (default) или JSON. Baseline-снимок —
  `docs/quality_report_baseline_2026_05_24.md`.
- **`tests/fixtures/discord_snapshots/`** — 7 known-good embed cases
  + раннер `tests/test_discord_alert_gallery.py`. UX regression-guard:
  любое изменение в discord/embed_builder валит diff. Update workflow:
  `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py`.

## 3f. Слой свидетельств и инцидентов (v1.0.4 – v1.0.8)

Глубокое ревью 05.09.2026 (10 PR, затем серия roadmap) изменило то, что граф *означает*, а не только то, что хранит. Контракты — в [`SEMANTIC_CONTRACT.md`](SEMANTIC_CONTRACT.md) §5.1 и §11–§13.

### SourceStatus — синк, который ничего не увидел, не считается отработавшим
Каждая задача-источник возвращает `SourceStatus` ∈ `success / partial / empty / unavailable / failed / invalid` (`app/knowledge_graph/source_status.py`). Heartbeat пишут только `success` / `partial`; сам статус пишется в Redis всегда (`stats:beat:last_run:status:<task>`) и показывается проверкой `sync_lag` как `last_status`. Источник, выключенный настройкой, отдаёт `disabled`, а не `warn`. Сторож: `tests/test_data_sources_are_watched.py::test_every_data_source_declares_its_status`.

### Evidence-контракт — три исхода, не два
`Fact.verdict` ∈ `found / absent / unknown`. `unknown` порождается только `Fact.unknown(kind, reason)` (confidence принудительно 0, `epistemic=unknown`, `unknown_reason` обязателен). Каждое `Rule` объявляет `sources` — поля ctx, от которых зависит ответ, — а `Rule.run()` понижает `absent` до `unknown`, если любой из них помечен в `ctx["source_status"]`; `found` не понижается никогда. `alert_enrichment.enrich_alert` заполняет `source_status` при исключении запроса, при мёртвом потоке деплоев (`deploy_stream_freshness().stale`, теперь и в сервисном пути) и когда сервиса нет в графе. Потребители обязаны различать: `absent` может опровергать гипотезу, `unknown` — нет (`fact_critic` пропускает unknown-якоря; `to_prompt_context()` рисует `?` с предупреждением). Привязка деплоя стала свидетельством: `recent_deploys_for*` отдают `attribution_scope` (`service` / `namespace` / `unknown`) — точная запись `observed 0.95`, ns-broadcast `inferred 0.6`.

### Идентичность workload'а — `k8s_uid` и `incarnation`
`kg_services.k8s_uid`, `incarnation`, `incarnation_changed_at`: Deployment, пересозданный под тем же именем, — другой объект; `incarnation` растёт внутри `ON CONFLICT` при смене uid. `TargetRef.uid / incarnation` несут это в remediation.

### `kg_deploy_watch` — выкаты, увиденные кластером
Второй источник `kg_deployments`: Redis-снимок каждого workload'а (`uid`, `template_hash = sha256(spec.template)`, образы); выкат — смена образов или hash шаблона, а **не** `metadata.generation`, который HPA и внешние аннотации крутят каждый тик (hotfix 1.0.5: 10 064 ложных записей на 776 сервисов за 40 минут). Записи ложатся на service-узел с `namespace_scope=false`, поэтому `stale_class='active'` стал достижим (0 → 77 сервисов).

### Incident — один открытый инцидент на сервис
`kg_incidents` (`app/knowledge_graph/incidents.py`): `attach_alert` присоединяет firing-алерт к открытому инциденту сервиса, переоткрывает закрытый меньше 30 минут назад (`reopened_count`) или заводит новый; инвариант «≤ 1 открытого на (namespace, service)» — **частичный уникальный индекс**, а не код приложения. `kg_alerts.incident_id` хранит `incident_key` (`ns/service@время`). `reconcile_incidents` (beat) закрывает, когда все алерты resolved, и старит после 7 дней тишины. `noise=true`, когда обогащение сочло все алерты инцидента шумом (gen-mismatch / meta / rollout, виды per-алерт в `extras.noise_fingerprints`); настоящий алерт снимает флаг; `GET /kg/incidents` скрывает шум без `include_noise=true`. Корреляция между сервисами намеренно вне scope до месяца данных — она придёт рёбрами между инцидентами, а не размытием инварианта.

### Timeline — шесть таблиц на одной оси, плюс что копилот сделал
`incident_timeline.build_timeline`: инцидент, алерты, деплои (привязка как свидетельство), события подов, аномалии (по метрике и часу, всегда `inferred`), логи error-уровня в окне `[opened − 60 мин, (resolved | now) + 30 мин]`, порядок — по времени, при равном — причина раньше следствия. **Операционная память** (v1.0.8) продолжает ту же ось из `incidents.analysis` и `kg_remediation_decisions` по fingerprint: `evidence` (факты по вердиктам), `diagnosis` (`inferred`), `decision` (`declared`), `action.applied` (`observed`), `verification` (`observed` для verified/failed, `unknown` для pending); `memory.outcome` различает `action_verified`, `action_applied_unverified`, `resolved_without_action`, `resolved_without_analysis`. `unknowns` называет источники, которые опросить было нечем — пустая полоса никогда молча не равна «ничего не было».

### Blast radius — что сломается, если X сломается
`blast_radius.blast_radius_v2`: BFS *против* `calls` / `uses_db` / `uses_nats` / `serves_traffic` до двух шагов; каждое ребро классифицирует `epistemic.classify_edge` (источники ∪ `discovered_by`, свежесть, противоречия вроде `serves_traffic` с `endpoints_ready == 0`); пострадавший наследует слабейшее звено пути, `contradicted` на любом ребре помечает весь путь. `unknowns`: нет рёбер `calls` («неизвестны, а не отсутствуют»), вызывающие известны лишь из манифестов/env (все `calls` в графе declared/inferred — runtime-наблюдений пока нет), пропущенные `inactive`, горизонт, противоречивые пути. Старые ключи `blast_radius_for` сохранены для Discord-embed, который теперь показывает эпистемическую легенду (✓ ◇ ≈ ⌛ ⚠).

### Верификация remediation — та же цель, сработало, стало лучше
`remediation/verification.py`: перед записью `snapshot_target` читает живой объект (`uid`, `generation`, `observedGeneration`, `sha256(spec.template)`, реплики) через read-only `kubectl get` в `kubectl_breaker.run_kubectl`; если `target_ref.uid` из графа отличается — `apply_intent` отказывает (`target_reincarnated`). Второй снимок — после записи; оба в `executor_applied`. `schedule_verification` ставит `remediation_verify` на +5 и +15 минут (`REMEDIATION_VERIFY_DELAYS_SEC`); `assess()` проверяет идентичность, эффект (шаблон/generation для рестарта, реплики для scale), сходимость, здоровье, резолв алерта и новые CrashLoop/OOM-события подов; исход → `executor_state` `verified` / `verification_failed`; каждая проверка — `True / False / None`, и `None` значит «не смогли проверить», а не «ок».

### Self-health в метриках
`SelfHealthCollector` экспортирует `copilot_self_health_status`, `copilot_self_health_check_status{check}`, `copilot_self_health_last_run_timestamp` из Redis-снимка; `VMPodScrape` + `VMRule sre-ai-copilot` (`k8s/monitoring.yaml`) алертят на протухшие/отсутствующие/упавшие/зависшие в warn проверки. Счётчики `diagnostic_facts_verdict_total{kind, verdict}` и `remediation_verification_total{outcome}` измеряют Known Unknowns и исходы верификации напрямую.

## 4. Fact-Anchored Reasoning (рассуждение на основе фактов)

Движок диагностики запускается до любого LLM-агента. Он оценивает детерминированные правила по структурированным k8s-данным и выдаёт `FactStore` — типизированную коллекцию объектов `Fact`, каждый с полями:

- `kind`: канонический slug (`FactKind.OOM_KILLED`, `FactKind.PROCESS_CRASH`, …)
- `observed`: подтверждён ли факт
- `confidence`: 0.0–1.0 (снижается при конфликте)
- `evidence`: словарь с supporting-данными

`FactStore.conflicts()` детектирует `MUTUALLY_EXCLUSIVE_PAIRS` (например, `{oom_killed, process_crash}`), `_apply_conflict_signals()` снижает конфиденс до 0.60, добавляет `conflict_with` в evidence и добавляет блок `<conflicts>` в контекст промпта, видимый всем агентам.

LLM-агенты получают FactStore сериализованным в XML-блоке `<facts>`. FactCriticAgent использует его для adversarial grounding — гипотеза, противоречащая подтверждённым фактам, отклоняется.

## 5. Поток данных (Copilot-разговор)

```
POST /copilot
  → сохранение разговора
  → Celery task: generate_reply
  → состояние: INVESTIGATING
  → context builder
  → до 3 итераций анализа (порог конфиденса 0.7)
  → состояния: HYPOTHESIS_GENERATED → FIX_PROPOSED
  → опциональное Discord-уведомление
```

## 6. Детекция рецидивов

`SimilarIncidentEngine` запрашивает KG на предмет прошлых инцидентов того же сервиса, где:
- `resolution_quality = "resolved"` (quality-gate: прошёл `_is_quality_cause`)
- `resolved_at >= now() - RECURRENCE_WINDOW_DAYS` (по умолчанию 7 дней)

При совпадении устанавливается `recurrence=True`. `FixAgent` переключается в investigative-режим (`_RECURRENCE_PREFIX`) — не рекомендует простой рестарт, так как это уже применялось.

## 7. Надёжность и наблюдаемость

- На критических задачах настроены Celery retries; каждая beat-задача держит аренду `single_instance`; тяжёлые синки — в своей очереди (§2.2).
- `/readyz` проверяет доступность PostgreSQL запросом `SELECT 1`.
- Prometheus-метрики: API на :8001 (латенси запросов, счётчики вебхуков, collector-ы self-health и длины очередей), воркеры на :8001 в multiprocess-режиме (`app/observability/worker_metrics.py`); скрейпит `VMPodScrape sre-ai-copilot`, алерты — `VMRule sre-ai-copilot` (`k8s/monitoring.yaml`). Перечень — `docs/METRICS.ru.md` §7.
- OpenTelemetry-трейсинг инициализируется при старте, если задан `OTLP_EXPORTER_ENDPOINT`.
- Логирование — §2.7; audit log — JSON-строки в stdout (в файл только при заданном `AUDIT_LOG_PATH`).
- Self-health, еженедельная сверка, ретеншен, бэкап — §2.5–§2.6.

## 8. Безопасность

- JWT-based dependency для `/copilot`.
- HMAC-валидация через `ALERTMANAGER_WEBHOOK_SECRET` для вебхуков Alertmanager; `REMEDIATION_WEBHOOK_SECRET` (HMAC над `timestamp.body`, anti-replay) для `/webhooks/remediation`.
- Discord interactions: проверка подписи Ed25519 по `DISCORD_PUBLIC_KEY`; клики приходят через relay ai-reviewer, NetworkPolicy пускает только из `ai-reviewer-net` (§2.3).
- Запись в кластер — только из `copilot-executor` под своим ServiceAccount `sre-ai-executor`: Role `sre-ai-remediate` (`deployments`, `deployments/scale`: get/list/patch/update) биндится по namespace только на этот SA (`k8s/base/rbac.yaml`); API и воркеры работают под `sre-ai` с правами только на чтение.
- Guardrails на уровне DSL (`ExecutionIntent`) и валидатора k8s-политик (`K8sSecurityGuard`).
- Approval API для human-in-the-loop перед любым write-действием.
- `SAFE_MODE=true` принудительно в production (config validator бросает исключение при `SAFE_MODE=false` + `ENV=production`).
- Защита от prompt injection (`prompt_guard.detect_injection`) с лимитом на длину входа (`PROMPT_INPUT_MAX_CHARS`).
- **PII redaction** (`app/services/pii_redaction.py`) — write-time scrubbing log-сэмплов Seq и defense-in-depth scrubbing строк Discord embed. Паттерны: email, IPv4/IPv6, JWT, bearer, UUID, длинный hex, `password|token|secret|api_key` key/value.
- **Approve / Decline authz** на Discord-кнопках — allowlists `DISCORD_APPROVERS_USER_IDS` / `DISCORD_APPROVERS_ROLE_IDS` (в проде только список пользователей), квота `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR`. Fail-closed когда оба списка пустые.
- **Выделенная read-only Postgres-роль** (`kg_reader`) для внешнего доступа к KG — отдельный user, `SELECT` только на `kg_*` и `alembic_version`, no default privileges (будущие таблицы нужно грантить руками).

## 9. Внешние интеграции

| Интеграция | Назначение | Config-ключи |
|---|---|---|
| Kubernetes (MCP) | Состояние подов, логи, события, управление деплоем | через wo-tools MCP |
| VictoriaMetrics | Метрики памяти/CPU за N минут до инцидента | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Контекст последних деплоев | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Известные открытые/закрытые тикеты по сервису | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Карточки алертов, дайджесты | `DISCORD_WEBHOOK_URL` |
| Discord (бот) | Карточки предложений с кнопками, interactions | `DISCORD_BOT_TOKEN`, `DISCORD_INCIDENT_CHANNEL_ID`, `DISCORD_PUBLIC_KEY` |
| Discord (per-team) | Per-team channel routing для digests/incidents | `DISCORD_TEAM_CHANNEL_MAP` (JSON) |
| Discord (self-health) | KG canary алерты, отдельно от `#infra-error` | `DISCORD_WEBHOOK_SELF_HEALTH_URL` |
| Discord (authz) | Allowlist для Approve/Decline кнопок | `DISCORD_APPROVERS_USER_IDS`, `DISCORD_APPROVERS_ROLE_IDS`, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` |
| Seq | Поток application-логов → `kg_log_observations` | `SEQ_INSTANCES` или `SEQ_URL_<ENV>` / `SEQ_TOKEN_<ENV>` |
| squad-medic (внешний) | События прогонов лечения → `kg_remediation_events` | `REMEDIATION_WEBHOOK_SECRET` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
| Anomaly tuning | Robust-z пороги | `KG_ANOMALY_ROBUST_Z_WARN`, `KG_ANOMALY_ROBUST_Z_CRIT` |
