# Документация по модулям

## API-слой
- `app/main.py`: инициализация FastAPI, middleware, роутеры, health/readiness, async job endpoints.
- `app/api/webhooks.py`: endpoint для AlertManager webhook и статус фоновой задачи.
- `app/api/replay.py`: повторный запуск анализа по историческому `incident_id`.
- `app/api/approvals.py`: approve/reject/get для approval workflow.
- `app/evaluation/feedback.py`: прием пользовательского feedback и агрегированная статистика.

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
- `Service` — узлы (`kg_services`, unique `(namespace, name)`); флаг `synthetic` скрывает инфра/observability/drift-узлы из KG-запросов; `team_owner` выводится из namespace prefix.
- `ServiceEdge` — направленные рёбра (`kg_service_edges`); `kind` ∈ `calls` / `uses_nats` / `uses_db`; `last_seen_at` для TTL/decay; `extras.discovery_sources` (list) копит все источники (multi-source = выше confidence).
- `Deployment` — история TC builds (`kg_deployments`); `started_at` из TC API (не `finishDate`), `triggered_by`, статус `SUCCESS`/`FAILURE`/etc.
- `AlertEvent` — алерты от AM (`kg_alerts`); идемпотентно по `fingerprint`; `resolved_at` обновляется через `kg_alerts_resolve_sync`.
- `PodEvent` — k8s Warning-события (`kg_pod_events`); идемпотентно по `event_uid`; `count` копит kubelet-retries.

### Sync (auto-populating beat tasks)
- `app/knowledge_graph/kg_sync.py`: `sync_topology()` — раз в час `kubectl get deployments -A` → `kg_services` + рёбра из env-vars (HTTP URLs, NATS-кластеры) и `secretKeyRef.key` heuristic (DB-DSN без чтения значений secret).
- `app/knowledge_graph/k8s_events_sync.py`: `sync_all_events()` — каждые 10 мин `kubectl get events --field-selector type=Warning` → `kg_pod_events`. Pod-name → service резолвится regex'ом стандартного k8s pod-hash паттерна.
- `app/knowledge_graph/k8s_ingress_sync.py`: `sync_all_ingresses()` — раз в час `kubectl get ingresses -A` → synthetic-узлы `ingress:<host>` + рёбра `calls` к backend-сервисам.
- `app/knowledge_graph/alerts_resolve_sync.py`: `run_alerts_resolve_sync()` — каждые 15 мин сравнивает `kg_alerts.fingerprint` с `GET AM /api/v2/alerts` → не-firing → `resolved_at=NOW`. Safety: min 1 active fingerprint.
- `app/knowledge_graph/drift_cleanup.py`: `run_drift_cleanup()` — раз в час; services из несуществующих ns → `synthetic=true` + `metadata.drift_reason`. Safety threshold 20% drift_pct.

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
- `app/services/discord_service.py`: отправка уведомлений в Discord.
- `app/services/prompt_guard.py`: детекция prompt injection с лимитом на длину входа.

## Observability
- `app/telemetry.py`, `app/observability/*`: трассировка, AI-метрики, структурированное логирование.
- `app/metrics.py`: Prometheus-метрики приложения.
