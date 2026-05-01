# Module Documentation

## API Layer
- `app/main.py`: инициализация FastAPI, middleware, роутеры, health/readiness, async job endpoints.
- `app/api/webhooks.py`: endpoint для NewRelic webhook и статус фоновой задачи.
- `app/api/replay.py`: повторный запуск анализа по историческому `incident_id`.
- `app/api/approvals.py`: approve/reject/get для approval workflow.
- `app/evaluation/feedback.py`: прием пользовательского feedback и агрегированная статистика.

## Workers & Orchestration
- `app/workers/tasks.py`: Celery-задача `process_incident` и полный агентный pipeline.
- `app/celery_worker.py`: Celery-задача `generate_reply`, state transitions, итеративный confidence loop.
- `app/core/state_machine.py`: допустимые состояния и переходы жизненного цикла инцидента.

## Agents
- `app/agents/analyzer.py`: первичный анализ контекста/инцидента.
- `app/agents/hypothesis.py`: генерация гипотез.
- `app/agents/critic.py`: критический аудит гипотез и выбор причины.
- `app/agents/fix.py`: предложение remediation.
- `app/agents/risk.py`: оценка рисков предлагаемого remediation.

## Context & Intelligence
- `app/context/context_builder.py`: сбор и нормализация обогащенного контекста.
- `app/context/logs.py`, `metrics.py`, `deployments.py`: адаптеры источников контекста.
- `app/core/intelligence/*`: вспомогательные функции анализа (blast radius, temporal diff, next steps, similar incidents).

## Data & Persistence
- `app/database.py`, `app/db/*`: engine/session helpers и интеграция БД.
- `app/models/*` и `app/models.py`: Pydantic/ORM-модели домена.
- `app/repository.py`: CRUD-операции разговоров/сообщений.

## Services & Safety
- `app/services/approval_manager.py`: Redis-based lifecycle approvals.
- `app/services/k8s_guard.py`: policy-check операций (verb/resource/namespace/body).
- `app/core/execution_dsl.py`: строго типизированный `ExecutionIntent` и kubectl-транслятор.
- `app/services/resilience.py`: retry/circuit breaker логика вокруг LLM вызовов.
- `app/services/discord_service.py`: отправка уведомлений в Discord.

## Observability
- `app/telemetry.py`, `app/observability/*`: трассировка, метрики AI, логирование.
- `app/metrics.py`: Prometheus-метрики приложения.
