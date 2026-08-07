# Документация по модулям

## API-слой
- `app/main.py`: инициализация FastAPI, middleware, роутеры, health/readiness, async job endpoints.
- `app/api/webhooks.py`: endpoint для AlertManager webhook и статус фоновой задачи.
- `app/api/replay.py`: повторный запуск анализа по историческому `incident_id`.
- `app/api/approvals.py`: approve/reject/get для approval workflow.
- `app/evaluation/feedback.py`: прием пользовательского feedback и агрегированная статистика.

### Discord interactions (`app/api/discord_interactions.py`)
- Discord application-interactions endpoint — принимает клики Approve / Decline с incident-embed'а.
- **Authorization gate (fail-closed):**
  - Allow если `interaction.user.id ∈ DISCORD_APPROVERS_USER_IDS`, ИЛИ
  - Allow если `member.roles ∩ DISCORD_APPROVERS_ROLE_IDS ≠ ∅`.
  - Иначе — ephemeral deny + audit `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
  - Если оба allowlist'а пустые: deny с `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`. Система никогда не «авто-разрешает по отсутствию политики».
- **Rate-limit:** in-memory, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` на Discord-юзера (default 5). Authz-fail НЕ потребляет квоту — заблокированного юзера нельзя использовать для исчерпания лимита легитимного approver'а.
- **Persistence:** при accept'е пишет одну `ActionApproval` строку (UNIQUE `(incident_id, intent_signature)`). Конкурентные клики коллизионятся на UNIQUE, второй получает «already approved/declined by @user» ephemeral.

## Workers и оркестрация
- `app/workers/tasks.py`: Celery-задача `process_incident` — полный 8-стадийный агентный пайплайн: DiagnosticsEngine, MultiHypothesis, FactCritic, Jira enrichment, Fix, Risk, обновление KG.
- `app/celery_worker.py`: Celery-задача `generate_reply`, переходы состояний, итеративный confidence loop.
- `app/core/state_machine.py`: допустимые состояния и переходы жизненного цикла инцидента.

## Движок диагностики
- `app/diagnostics/engine.py`: `DiagnosticsEngine` — оценивает все зарегистрированные правила против k8s-контекста, применяет conflict signals через `_apply_conflict_signals()`, возвращает заполненный `FactStore`.
- `app/diagnostics/facts.py`: `FactKind` (канонические слаги), `Fact`, `FactStore`, `MUTUALLY_EXCLUSIVE_PAIRS`, `FactStore.conflicts()`, `FactStore.to_prompt_context()`.
- `app/diagnostics/rules/oom.py`: `OOMKilledRule` — сначала структурный шлюз (`_check_pod_state()`), text-regex fallback только при отсутствии exit code; возвращает `observed=False` если целевой exit ≠ 0 и ≠ 137.
- `app/diagnostics/rules/crash.py`: `ProcessCrashRule` — детектирует SIGSEGV/SIGABRT/ненулевые exit codes.
- `app/diagnostics/rules/crashloop.py`: `CrashLoopRule` — детектирует состояние CrashLoopBackOff.
- `app/diagnostics/rules/scheduling.py`: `FailedSchedulingRule` — детектирует ошибки планирования (ресурсы/taint/affinity).
- `app/diagnostics/rules/deploy.py`: `RecentDeployRule` — коррелирует время деплоя TeamCity с инцидентом.

## Агенты
- `app/agents/base.py`: `BaseAgent` — общий метод `ask()`, подключающий LLM backend.
- `app/agents/analyzer.py`: первичный анализ контекста/инцидента.
- `app/agents/multi_hypothesis.py`: `MultiHypothesisOrchestrator` — параллельный fan-out по перспективам (app/infra/deps/runtime), отфильтрованным по `PERSPECTIVE_PRECONDITIONS`; собирает `HypothesisResult` с флагом `survived`.
- `app/agents/fact_critic.py`: `FactCriticAgent` — adversarial grounding каждой гипотезы против `FactStore`; устанавливает `survived=True/False`.
- `app/agents/fix.py`: `FixAgent` — генерирует JSON `ExecutionIntent`; `_RECURRENCE_PREFIX` для recurrence-режима; `_build_jira_prefix()` для Jira-обогащения.
- `app/agents/risk.py`: оценка рисков предлагаемого remediation.

## Контекст и интеллект
- `app/context/context_builder.py`: сборка и нормализация обогащённого контекста из всех источников.
- `app/context/logs.py`, `metrics.py`, `deployments.py`: адаптеры k8s-логов, VM-метрик, истории деплоев.
- `app/context/jira_client.py`: `JiraClient` (Atlassian REST API v3, Basic Auth); `build_jira_context()` → `{open, resolved, has_open, has_resolved, total}` или `None`.
- `app/core/intelligence/similar_incidents.py`: `SimilarIncidentEngine` — поиск по KG с фильтром `_is_quality_cause()`, `RECURRENCE_WINDOW_DAYS=7`, флаг `recurrence` в каждом результате.
- `app/core/intelligence/blast_radius.py`, `temporal_diff.py`, `next_steps.py`: вспомогательные аналитические функции.

## Knowledge Graph

### Schema (`app/knowledge_graph/schema.py`)
- `Service` — узлы (`kg_services`, unique `(namespace, name)`); флаг `synthetic` скрывает инфра/observability/drift-узлы из KG-запросов; `team_owner` выводится из namespace prefix; `stale_class` (PR #86): `active` / `expected_stale` / `suspicious_stale` (см. `stale_classifier`).
- `ServiceEdge` — направленные рёбра (`kg_service_edges`); `kind` ∈ `calls` / `uses_nats` / `uses_db` / `serves_traffic` / `routes_to`; `last_seen_at` для TTL/decay; `extras.discovery_sources` (list) копит все источники (multi-source = выше confidence).
- `Deployment` — история TC builds (`kg_deployments`); `started_at` из TC API (не `finishDate`), `triggered_by`, статус `SUCCESS`/`FAILURE`/etc.
- `AlertEvent` — алерты от AM (`kg_alerts`); идемпотентно по `fingerprint`; `resolved_at` обновляется через `kg_alerts_resolve_sync`.
- `PodEvent` — k8s Warning-события (`kg_pod_events`); идемпотентно по `event_uid`; `count` копит kubelet-retries.
- `K8sJob` (PR #82, `kg_k8s_jobs`) — Job/CronJob узлы вне `kg_services`; `kind` ∈ `job` / `cronjob`; `owner_service_id` метаколонка реализует semantic edge `runs_as_job` без отдельного edge-row.
- `StorageVolume` (PR #84, `kg_storage_volumes`) — PVC/PV узлы; `kind` ∈ `pvc` / `pv` (PVC — namespace-scoped, PV — cluster-scoped с `namespace=''`); атрибуты: phase, capacity_bytes, storage_class.
- `VolumeEdge` (PR #84, `kg_volume_edges`) — heterogeneous edges (`uses_volume` Service→PVC, `bound_to` PVC→PV); tagged `src_kind`/`dst_kind` без FK constraint.
- `IngressObservation` (`kg_ingress_observations`) — per-endpoint (host/path) HTTP-снапшоты: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_5xx_rate` / `error_4xx_rate`, `service_id` FK на backend-сервис; `UNIQUE (ingress_name, host, path, ts)`. Наполняется с 2026-06-10 (см. Ingress observations sync).

### Schema / quality contract (`app/knowledge_graph/contract.py`)
- `KG_SCHEMA_VERSION` — текущая версия (`2.4`, после `kg_services.node_kind` 2026-08-07: k8s Service и workload — разные типы узлов). Bump rules — `docs/KG_SCHEMA_CONTRACT.md` §8.
- `EDGE_KINDS` — реестр всех edge kinds + spec (`semantic` / `src_kinds` / `dst_kinds` / `source` / `status` / `table`). `table` = где edge живёт: `kg_service_edges` / `kg_volume_edges` / `fk_only` (через FK) / `metadata_only` (через owner_service_id).
- `OWNER_SOURCES` / `OWNER_SOURCE_ALIASES` — canonical источники owner-а + маппинг коротких имён из `ownership_suggester`.
- `STALE_CLASS_VALUES` — enum значений `kg_services.stale_class`.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика drift-а.
- Test: `tests/test_contract_drift.py` (Gate #22) — auto-validation что код и контракт не разъезжаются.

### Sync (auto-populating beat tasks)
- `app/knowledge_graph/kg_sync.py`: `sync_topology()` — раз в час `kubectl get deployments -A` → `kg_services` + рёбра из env-vars (HTTP URLs, NATS-кластеры) и `secretKeyRef.key` heuristic (DB-DSN без чтения значений secret).
- `app/knowledge_graph/k8s_events_sync.py`: `sync_all_events()` — каждые 10 мин `kubectl get events --field-selector type=Warning` → `kg_pod_events`. Pod-name → service резолвится regex'ом стандартного k8s pod-hash паттерна.
- `app/knowledge_graph/k8s_ingress_sync.py`: `sync_all_ingresses()` — раз в час `kubectl get ingresses -A` → synthetic-узлы `ingress:<host>` + рёбра `calls` к backend-сервисам.
- `app/knowledge_graph/alerts_resolve_sync.py`: `run_alerts_resolve_sync()` — каждые 15 мин сравнивает `kg_alerts.fingerprint` с `GET AM /api/v2/alerts` → не-firing → `resolved_at=NOW`. Safety: min 1 active fingerprint.
- `app/knowledge_graph/drift_cleanup.py`: `run_drift_cleanup()` — раз в час; services из несуществующих ns → `synthetic=true` + `metadata.drift_reason`. Safety threshold 20% drift_pct.
- `app/knowledge_graph/k8s_topology_resources_sync.py` (Wave 7-X): каждые 15 минут `kubectl get services/ingresses -A -o json` → edges `serves_traffic` (Service → backing Deployment по selector-match) и `routes_to` (Ingress-как-ресурс → backend Service). Параллельный slice к `k8s_ingress_sync` (Ingress-as-host). RBAC: cluster-role требует `services`, `ingresses` на verbs `get`/`list`/`watch`.
- `app/knowledge_graph/runtime_correlation.py` (Wave 7-Y): каждые 30 минут sliding-window по `kg_pod_events`. Для каждой пары `(src, dst)` где edge уже существует, считает co-occurrences warning-событий в окне (default 15 мин). При count ≥ MIN_COUNT — подтверждает edge новым `discovery_source = "kg_sync/runtime_corr"` (tier-1 precedence 0.95). Не создаёт новые edges (симметричный сигнал не определяет direction). Feature flag `RUNTIME_CORRELATION_ENABLED=true`.
- `app/knowledge_graph/nats_subjects_sync.py` (Wave 7-Z): каждые 6 часов shallow clone + sparse-checkout WO monorepo, regex-парсер C# исходников на consumers (`NatsJetStreamConsumer<T>`) и publish call-sites (`SendToJetStreamAsync`). Subject = synthetic-Service в `nats-subjects` namespace, `name=subject:<value>`. Edge `uses_nats` с `extras.direction ∈ {pub, sub}`, `weight = count(call-sites)`. Feature flag `NATS_SUBJECTS_PARSER_ENABLED=false` (требует ssh-доступ к gitlab-monorepo).
- `app/knowledge_graph/k8s_jobs_sync.py` (Wave 8-A, PR #82): `sync_k8s_jobs()` — каждые 15 минут `kubectl get jobs,cronjobs -A -o json` → `kg_k8s_jobs`. Поля Jobs: `succeeded_count`/`failed_count`/`active_count`/`completion_time`/`last_pod_exit_code`. Поля CronJobs: `schedule`/`last_schedule_time`/`last_successful_time`/`suspended`. Owner Service резолвится через label `app.kubernetes.io/part-of` (fallback `app`) → semantic `runs_as_job` через `owner_service_id` metadata-column (а не отдельный edge-row).
- `app/knowledge_graph/k8s_storage_sync.py` (Wave 8-B, PR #84): `sync_storage()` — каждые 30 мин, отдельные проходы для PV (cluster-scoped), PVC (namespace-scoped) + scan `pod.spec.volumes` для `uses_volume` edges. Все edges идут в `kg_volume_edges` (heterogeneous, tagged src/dst). Опциональный disk_pct enrichment через `kubelet_volume_stats_*` PromQL под флагом `STORAGE_METRICS_ENABLED=false` (default OFF — scrape config может быть не настроен).
- `app/knowledge_graph/stale_classifier.py` (Wave 8-D, PR #86): `classify_stale_with_deploys(name, ns, last_deploy_at, team_owner)` → `active` / `expected_stale` / `suspicious_stale`. Используется `kg_sync.sync_namespace` для апдейта `kg_services.stale_class` идемпотентно. Re-exports canonical values из `contract.STALE_CLASS_VALUES`.

### Population (`app/knowledge_graph/populator.py`)
Идемпотентные upsert'ы, используются всеми sync'ами:
- `upsert_service(namespace, name, team_owner, synthetic, metadata)` — идемпотентно по `(namespace, name)`.
- `upsert_edge(src, dst, kind, discovered_by, extras)` — обновляет `last_seen_at` и merge `discovery_sources` (unique-preserved-order list).
- `record_deployment` / `record_alert_event` / `record_pod_event` — идемпотентны по соответствующим natural keys.

### Queries (`app/knowledge_graph/queries.py`)
- `recent_deploys_for(ns, svc, before, lookback_minutes)` — deploy-записи с `triggered_by` и TC build URL.
- `upstream_of(ns, svc, kinds=None, fresh_only_days=N)` — outgoing edges с `confidence_score`/`confidence_label`.
- `incidents_on` / `nearby_alerts` / `recent_pod_events_for` — оставшиеся read-side queries для enrichment.

### Confidence (`app/knowledge_graph/confidence.py`)
- `confidence_score(extras, last_seen_at)` → [0, 1]. Формула: `base × source_count_mul × freshness_mul`.
- `confidence_label` (`high`/`medium`/`low`) + `confidence_badge` (`●●●`/`●●○`/`●○○`).
- LLM-readiness: при включении LLM-пайплайна модель видит «inferred с env+url confidence 0.7».

### Alert enrichment (`app/services/alert_enrichment.py`)
- `enrich_alert(db, incident)` → `EnrichedContext`. Синхронно, ~5 SQL-запросов, **без LLM**. Путь: `/webhooks/alertmanager/enrich-and-forward`.
- Adaptive `effective_at = max(starts_at, now-24h)` — для длительных хроник anchor на `now`.
- `primary_hypothesis()` — top-1 observed Fact; `why_this_matters()` — derived priorization (shared dep, chronic, recurrence, infra-critical team).

### CLI tools (`app/scripts/`)
- `backfill_team_owner.py` — one-shot UPDATE `team_owner` для legacy строк.
- `backfill_tc_deploys.py` — расширенный TC history backfill (default 30 дней).
- `cleanup_drift.py` — thin wrapper над `drift_cleanup.py` для ручного dry-run / apply.

### Metrics sync (`app/knowledge_graph/metrics_sync.py`)
- `sync_service_health()` — каждые 5 мин, выполняет пять PromQL-запросов на сервис (`cpu_pct`, `mem_pct`, `restarts_rate`, `http_5xx_rate`, `p95_latency_ms`) против VictoriaMetrics и upsert'ит в `kg_service_health`.
- Идемпотентность: каждая запись оборачивается в Postgres `SAVEPOINT`; `IntegrityError` на UNIQUE `(service_id, ts)` откатывает savepoint, цикл продолжается. Повторный запуск на пересекающемся окне безопасен.
- **Wave 5 gotcha (читать ДО правок запросов):** `mem_pct` должен считаться как `avg(rate(working_set) / on(pod) container_limit)` — сначала делим, потом агрегируем. Форма "aggregate-then-divide" молча возвращает 0, потому что many-to-one join схлопывает результат до деления. Canary `materialization_zero_rate` в `kg_self_health` существует именно ради этого класса регрессий.
- Настраивается через env (VM query window, lookback, per-service timeout). Если VM недоступна — задача no-op'ит (следующий тик retry'ит); сигнал об упавших тиках — `kg_self_health.sync_lag`.
- **Семантика нулей (актуально на 2026-06-10):** `http_5xx_rate` и `p95_latency_ms` по-прежнему всегда 0 — `/metrics` ASP.NET-сервисов закрыт JWT (401), app-level скрейпа нет. Бэкенд-тикет **WO-12483**; после раскатки поля оживут через VMServiceScrape на app-namespace'ы. До тех пор `0` = «нет данных», НЕ «нет ошибок / быстро». Per-host/path HTTP-сигнал есть в `kg_ingress_observations` (наполняется с 2026-06-10, см. ниже); ingress-метрики больше не являются причиной этих нулей.

### Cluster health sync (`app/knowledge_graph/cluster_health_sync.py`)
- `sync_cluster_observations()` — каждые 5 мин, снэпшотит k8s node-уровень (Ready/SchedulingDisabled, allocatable vs requested CPU/mem, pod count) в `kg_cluster_observations`. Источник истины для cluster-wide capacity view.

### Ingress observations sync (`app/knowledge_graph/ingress_observations_sync.py`)
- `sync_ingress_observations()` — каждые ~10 мин (beat task `kg_ingress_observations_sync`), ingress-controller PromQL (`nginx_ingress_controller_*`) → `kg_ingress_observations`, одна строка на `(ingress_name, host, path)` с `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_5xx_rate` / `error_4xx_rate`. Идемпотентно по `UNIQUE (ingress_name, host, path, ts)`.
- **Наполняется с 2026-06-10:** метрики nginx-ingress включены на обоих DaemonSet-контроллерах кластера, скрейп — VMPodScrape с `honorLabels`. 100% рядов слинкованы с `kg_services` через `service_id` (backend резолвится из ingress rule).
- **Семантика нулей:** `error_5xx_rate = 0` при ненулевых `rps`/`p95` теперь означает реально «ошибок нет» — данные собираются. Ряды, где ВСЕ метрики = 0, sync пропускает (экспортёр не накрывает endpoint), поэтому «нет данных» проявляется как отсутствие ряда, а не как нули.
- **Разрез:** endpoint (host/path), НЕ per-service агрегат — не путать с `kg_service_health` (там `http_5xx_rate`/`p95_latency_ms` по-прежнему всегда 0, см. Metrics sync выше).

### Anomaly detection (`app/knowledge_graph/anomaly_detection.py`)
- `scan_anomalies()` — каждые 5 мин, читает `kg_service_health` и `kg_ingress_observations` (наполняется с 2026-06-10) и пишет `kg_anomaly_observations` для каждой `(service, metric)` где текущее значение статистически вне baseline. NB: app-level `http_5xx_rate`/`p95_latency_ms` в `kg_service_health` по-прежнему всегда 0 (app `/metrics` за JWT, WO-12483), поэтому app-слойные аномалии идут из `log_error_rate` (лог-производный прокси, НЕ HTTP 5xx) и из ingress observations.
- **Метод:** rolling robust-z, `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Устойчив к выбросам в самом baseline-окне.
- **Seasonal baseline:** при ≥50 исторических точках baseline стратифицируется по hour-of-day. `extras.method = robust_z_seasonal`. Иначе — `robust_z_flat`.
- **Пороги:** `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) → `warning` строка, `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`) → `critical`.
- **Volume guard:** не более 3 observations на (service, metric) в час. Защита от flood'а при затяжной аномалии.
- **Baseline window:** 7 дней rolling.
- `flat_baseline` пишется в `extras` если baseline весь нули — используется deploy correlator'ом для damp confidence ("спайк с 0" на мёртвой метрике — не сигнал).

### Deploy correlator (`app/rca/deploy_correlator.py`)
- Вызывается inline из `IncidentPipeline._enrich_deploy_correlation` для каждого инцидента, дошедшего до enrichment.
- **Окно:** `[incident_started_at − 2h, incident_started_at]`. Все `kg_deployment` сервиса в этом окне — кандидаты на причину.
- **Confidence formula** — взвешенный multi-factor score в `[0, 1]`:
  - `N_spikes` — число anomaly observations после деплоя
  - `max_zscore` — пиковый robust-z в окне
  - `time_proximity` — ближе по времени = выше
  - `deploy_status_factor` — FAILED больше, чем SUCCESS
  - `flat_baseline_penalty` — снижает confidence на спайках мёртвой метрики
- **Verdict tiers:** `likely` (≥ 0.7) / `suspect` (0.4–0.7) / `weak` (0.2–0.4) / `unlikely` (< 0.2).
- **Тюнинг:** пороги — константы в модуле; для ad-hoc настройки прогнать backfill на исторических инцидентах и посмотреть на `analysis["deploy_correlator"]["confidence"]` и `["factors"]` — какой фактор доминировал.

### Seq logs sync (`app/knowledge_graph/seq_logs_sync.py`)
- `sync_logs()` — каждые 10 мин, тянет события из Seq REST API за последнее 10-минутное окно и upsert'ит по одной строке на `(service, level)` в `kg_log_observations` (count + redacted sample).
- **Match-алгоритм:** Seq-property `Application` / `Service` → `kg_services.name`; fallback по namespace prefix.
- **PII redaction integration:** каждый `message_sample` прогоняется через `app/services/pii_redaction.py` *перед* записью, потом truncate до 500 символов. БД никогда не хранит сырой PII.
- **Известное ограничение:** при текущем layout'е реалмов все события приходят как `Information`. Чтобы заполнять `Error`/`Warning` независимо, нужны per-realm Seq или access-log routing. Схема и downstream-потребители уже level-aware.

### Signal aggregates (`app/knowledge_graph/signal_aggregates.py`)
- `aggregate_signals()` — hourly roll-up в окне 24 ч. Пишет одну строку на сервис в `kg_signal_aggregates` с пятью метриками:
  - anomaly count по severity
  - deploy count (SUCCESS / FAILURE)
  - alert event count
  - pod event count
  - log error rate
- Питает daily team digest (fragile top-5 ranking) и `health_score.py` (per-service composite).

### Multi-signal owner inference (`app/services/ownership_suggester.py`)
- Эвристика предложения owner-а для unowned-namespace. Используется в `stats_digest` для секции `Unowned namespaces`.
- **Архитектура multi-signal** — взвешенный fusion трёх независимых сигналов + manual override:
  - **A. Prefix** (weight 0.4) — regex-таблица по ns-имени (`squad-N-*` → `@squad-N`, `<env>-kingdom<N>` → `@kingdom-N`, bare `monitoring`/`kube-system` → `@platform`).
  - **B. Deploy history** (weight 0.4) — most-frequent `triggered_by` из `kg_deployments` за 30 дней. Username транслируется через `owner_aliases.resolve_username`. Покрывает кейс «ns без префикса, но один человек туда стабильно деплоит».
  - **C. Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` / `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
  - **Manual override** через `OWNERSHIP_MANIFEST_PATH=ownership.yaml` со списком `[{ns_pattern, owner, reason}]`. Match по pattern (glob) → confidence=1.0, эвристики игнорируются.
- API: `suggest_owner_multi_signal(db, ns) → OwnerSuggestion(owner, confidence, sources, reasoning)`. Top-1 по сумме `weight × signal_strength`. Confidence clamp `[0, 1]`.
- **Backward compat**: старая `suggest_owner_for_ns(ns)` оставлена как deprecated wrapper.

### Owner aliases (`app/services/owner_aliases.py`)
- TC username → team mapping для owner inference (сигнал B).
- **Источники маппинга** в порядке приоритета:
  1. YAML-файл из ENV `OWNER_ALIASES_PATH` (deployment-specific override).
  2. Pre-baked `_DEFAULT_ALIASES` в коде (подтверждено по recent_deploys digest-у).
  3. Fallback `@?-{username}` — caller возвращает для неизвестных.
- API: `resolve_username(username: str) → str` (lowercase'ит вход).

### Quality report (`app/scripts/quality_report.py`)
- Идемпотентный read-only CLI: 5 групп метрик из Postgres KG-БД (`kg_services`, `kg_service_edges`, `kg_volume_edges`, `kg_alerts`, `kg_deployments`, `kg_k8s_jobs`, `kg_storage_volumes`, `kg_pod_events`).
- **Use case**: точка отсчёта для Phase A (remediation), чтобы demonstrably улучшать метрики, а не угадывать. После 17 PR Wave 7 + Wave 8 нужен baseline-снимок перед Phase A.
- **Без записи в БД** — никаких INSERT/UPDATE/DELETE. Использует production `SessionLocal`; для unit-тестов `build_report(db)` принимает session-объект напрямую.
- **CLI**:
  ```bash
  python -m app.scripts.quality_report                    # markdown в stdout
  python -m app.scripts.quality_report --json             # JSON в stdout
  python -m app.scripts.quality_report --markdown --output baseline.md
  ```
- Baseline snapshot: `docs/quality_report_baseline_2026_05_24.md`.

### Self-health (`app/knowledge_graph/self_health.py`)
«Monitoring of the monitoring». Шесть canary'ев по KG data quality, запускаются каждые 30 минут beat-задачей `kg_self_health_check`.

| Check | Условие prохода |
|---|---|
| `materialization_zero_rate` | <X% строк со значением 0/NULL на метрику в `kg_service_health` за 24 ч. Allowlist для known-zero метрик. |
| `sync_lag` | На каждую beat-задачу `max(ts)` в пределах 2× ожидаемого интервала. |
| `anomaly_signal_health` | Кол-во anomaly observations за 24 ч в нормальном диапазоне (не 0, не >500). |
| `alerts_resolve_freshness` | Кол-во open-and-old `kg_alerts` ниже порога. |
| `pod_events_link_rate` | ≥80% `kg_pod_events` имеют resolved `service_id`. |
| `edges_freshness` | ≤30% `kg_service_edges` stale (`last_seen_at` >24 ч или NULL). |

- Output: audit log (`KG_SELF_HEALTH_OK` / `_WARN` / `_FAIL`), и при любом `fail`/`warn` — single embed в `DISCORD_WEBHOOK_SELF_HEALTH_URL` (отдельно от `#infra-error`).
- **Dedup:** 6-часовое окно на check на уровне beat-задачи — один и тот же failing canary не репостится, пока не сменит статус или не пройдут 6 ч.
- **Как добавить новую проверку:** написать функцию `_check_X(db) -> CheckResult` и добавить в список `ALL_CHECKS`. Каждая проверка read-only и самостоятельная.
- **Конфигурация:** пороги — константы в модуле; Discord-webhook берётся из `settings.DISCORD_WEBHOOK_SELF_HEALTH_URL` (None отключает Discord-вывод, audit-log остаётся).

## Данные и персистентность
- `app/database.py`, `app/db/*`: engine/session helpers и интеграция БД.
- `app/models/*` и `app/models.py`: Pydantic/ORM-модели домена.
- `app/repository.py`: CRUD-операции разговоров и сообщений.

## Сервисы и безопасность
- `app/services/mcp_client.py`: клиент для выполнения инструментов на внешних MCP-серверах.
- `app/services/teamcity_service.py`: интеграция с TeamCity для анализа деплоев через MCP.
- `app/services/approval_manager.py`: Redis-based lifecycle аппрувов.
- `app/services/k8s_guard.py`: policy-check операций (verb/resource/namespace/body).
- `app/core/execution_dsl.py`: строго типизированный `ExecutionIntent` и kubectl-транслятор.
- `app/services/resilience.py`: retry/circuit breaker логика вокруг LLM-вызовов.
- `app/services/discord_service.py`: отправка уведомлений в Discord. Отрисовывает incident-embed'ы, держит 30-мин dedup (PATCH через `webhook?wait=true`), severity routing, per-team channel routing (`DISCORD_TEAM_CHANNEL_MAP`), блоки suspect-deploy / log-error / anomaly / recurrence / linked-alerts и action row с Approve/Decline.
- `app/services/prompt_guard.py`: детекция prompt injection с лимитом на длину входа.

### PII redaction (`app/services/pii_redaction.py`)
- Pure regex-based редактор. Паттерны: email, IPv4, IPv6, JWT (`xxx.yyy.zzz` base64), `Bearer …` заголовки, UUID, длинные hex-строки, `password|token|secret|api_key` key/value.
- **Write-time** в `seq_logs_sync.py` — log lines редактируются до записи в `kg_log_observations.message_sample`. БД не хранит сырой PII. После редакции — truncate до 500 символов.
- **Defense-in-depth** на render-time в `discord_service.py` — каждая строка, идущая в webhook, прогоняется через редактор повторно, на случай если новый источник данных обойдёт write-time scrubbing.
- **Идемпотентность:** прогон по уже отредактированному тексту даёт тот же результат.

### Approval manager (`app/services/approval_manager.py`)
- Redis-based lifecycle аппрувов (existing). Теперь подкреплён persistent ORM-строкой `ActionApproval` (`kg_action_approvals`, см. `app/knowledge_graph/schema.py`) с `UNIQUE (incident_id, intent_signature)`.
- `intent_signature` — детерминированный хэш `ExecutionIntent` (action + resource + namespace + params), считается `app/services/intent_signature.compute_signature`. Одно действие = одна approval-запись; повторный клик по embed коллизионит UNIQUE-ключ, handler отвечает «already approved/declined by @user».
- Статус финальный при создании (`approved` | `declined`); промежуточного `pending` нет — либо кнопку нажали, либо нет.

## Observability
- `app/telemetry.py`, `app/observability/*`: трассировка, AI-метрики, структурированное логирование.
- `app/metrics.py`: Prometheus-метрики приложения.
