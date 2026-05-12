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
