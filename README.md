# SRE AI Copilot

**SRE AI Copilot** — backend-сервис для автоматизации incident response в Kubernetes: прием вебхуков, асинхронный анализ инцидентов, генерация гипотез/фиксов и контур human approval.

## Что умеет сервис
- Принимает события инцидентов через вебхук (`/webhooks/newrelic`) и ставит обработку в Celery.
- Выполняет агентный пайплайн (analyzer → hypothesis → critic → fix → risk).
- Хранит записи инцидентов и результаты анализа в PostgreSQL.
- Поддерживает replay-режим для исторических инцидентов (`/replay/{incident_id}`).
- Экспортирует health/readiness, Prometheus-метрики и OpenTelemetry-трейсинг.

## Технологический стек
- **API**: FastAPI
- **Очереди**: Celery + Redis
- **БД**: PostgreSQL + SQLAlchemy
- **Observability**: Prometheus, OpenTelemetry, structlog
- **Интеграции**: Discord webhook, Kubernetes guardrails, Gemini/OpenAI ключи в конфиге

## Быстрый старт

### 1) Требования
- Docker + Docker Compose
- Python 3.11+

### 2) Настройка окружения
Создайте `.env` и задайте минимум:
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `DATABASE_URL`
- `REDIS_URL`
- `DISCORD_WEBHOOK_URL`
- `JWT_PUBLIC_KEY` (если включена авторизация JWT)

### 3) Запуск
```bash
docker-compose up -d
```

Для локального API (без контейнера):
```bash
uvicorn app.main:app --reload --port 8000
```

## Основные API-эндпоинты
- `POST /webhooks/newrelic` — прием инцидента, постановка async-задачи.
- `GET /webhooks/status/{task_id}` — статус Celery-задачи вебхука.
- `POST /copilot` — запуск генерации ответа/анализа в фоне.
- `GET /jobs/{task_id}` — статус задачи `generate_reply`.
- `POST /approvals/{approval_id}/approve|reject` — подтверждение/отклонение действий.
- `POST /replay/{incident_id}` — повтор анализа исторического инцидента.
- `POST /evaluation/{incident_id}/submit` и `GET /evaluation/stats` — feedback-контур.
- `GET /healthz`, `GET /readyz` — liveness/readiness.

## Безопасность и guardrails
- AI не исполняет kubectl напрямую: используется `ExecutionIntent` DSL и детерминированный транслятор.
- Есть whitelist для K8s-операций (verb/resource) и blacklist системных namespace.
- Потенциально опасные действия проходят через approval flow.

## Документация
- [Architecture](docs/ARCHITECTURE.md)
- [Module Docs](docs/MODULE_DOCS.md)
- [Semantic Contract](docs/SEMANTIC_CONTRACT.md)
- [DR Plan](docs/DR.md)
