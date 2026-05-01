# SRE AI Copilot Architecture

## 1. System Overview
Приложение состоит из HTTP API (FastAPI), фоновых задач (Celery), слоя данных (PostgreSQL) и интеграций (Redis, Discord, Kubernetes).

Основные сценарии:
1. Ingestion инцидента через webhook.
2. Асинхронный анализ инцидента в worker.
3. Фиксация результата и отправка уведомлений.
4. Replay исторического инцидента без побочных эффектов (Discord/K8s).

## 2. Runtime Components
- **API app (`app.main`)**: маршрутизация, auth dependency, метрики, health/readiness.
- **Webhook pipeline (`app.api.webhooks` + `app.workers.tasks`)**: прием payload и агентный chain.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: фоновая генерация аналитического ответа с state machine.
- **Data layer (`app.database`, `app.repository`)**: SQLAlchemy-модели и операции.
- **Safety services**: approval manager, K8s guard, execution DSL.

## 3. Data Flow (Webhook Incident)
```text
NewRelic webhook
  -> POST /webhooks/newrelic
  -> IncidentRecord(status=PENDING)
  -> Celery task: process_incident
  -> Analyzer -> Hypothesis -> Critic -> Fix -> Risk
  -> IncidentRecord(status=COMPLETED, analysis=...)
  -> Discord report
```

## 4. Data Flow (Copilot Conversation)
```text
POST /copilot
  -> conversation persistence
  -> Celery task: generate_reply
  -> state transition: INVESTIGATING
  -> context builder
  -> up to 3 analysis iterations (confidence threshold 0.7)
  -> state transition: HYPOTHESIS_GENERATED -> FIX_PROPOSED
  -> optional Discord notification
```

## 5. Reliability & Observability
- Celery retries configured on critical tasks.
- `/readyz` проверяет доступность PostgreSQL запросом `SELECT 1`.
- Prometheus latency метрика собирается middleware.
- OpenTelemetry включается на старте приложения.

## 6. Security
- JWT-based user dependency для `/copilot`.
- Guardrails на уровне DSL (`ExecutionIntent`) и K8s policy validator (`K8sSecurityGuard`).
- Approval API для human-in-the-loop перед рискованными действиями.
