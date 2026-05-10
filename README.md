# SRE AI Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688)](https://fastapi.tiangolo.com/)
[![Celery](https://img.shields.io/badge/Celery-5.3-37814A)](https://docs.celeryq.dev/)

**SRE AI Copilot** — backend-сервис для автоматизации incident response в Kubernetes: прием Prometheus AlertManager-вебхуков, асинхронный анализ инцидентов через агентный LLM-пайплайн (analyzer → hypothesis → critic → fix → risk), guardrails по k8s-namespace и human-approval flow перед любым write-действием.

## Что умеет сервис
- Принимает события инцидентов через вебхук Prometheus AlertManager (`/webhooks/alertmanager`) и ставит обработку в Celery.
- Выполняет агентный пайплайн (analyzer → hypothesis → critic → fix → risk).
- Хранит записи инцидентов и результаты анализа в PostgreSQL.
- Поддерживает replay-режим для исторических инцидентов (`/replay/{incident_id}`).
- Экспортирует health/readiness, Prometheus-метрики и OpenTelemetry-трейсинг.

## Технологический стек
- **API**: FastAPI
- **Очереди**: Celery + Redis
- **БД**: PostgreSQL + SQLAlchemy
- **Observability**: Prometheus, OpenTelemetry, structlog
- **Интеграции**: Discord webhook, Kubernetes guardrails, Anthropic Claude API

## Быстрый старт

### 1) Требования
- Docker + Docker Compose
- Python 3.11+

### 2) Настройка окружения
Создайте `.env` и задайте минимум:
- `ANTHROPIC_API_KEY`
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
- `POST /webhooks/alertmanager` — прием AlertManager-батча, постановка async-задачи на каждый alert.
- `GET /webhooks/status/{task_id}` — статус Celery-задачи вебхука.
- `POST /copilot` — запуск генерации ответа/анализа в фоне.
- `GET /jobs/{task_id}` — статус задачи `generate_reply`.
- `POST /approvals/{approval_id}/approve|reject` — подтверждение/отклонение действий.
- `POST /replay/{incident_id}` — повтор анализа исторического инцидента.
- `POST /evaluation/{incident_id}/submit` и `GET /evaluation/stats` — feedback-контур.
- `GET /healthz`, `GET /readyz` — liveness/readiness.

## Безопасность и guardrails
- AI не исполняет kubectl напрямую: используется `ExecutionIntent` DSL и детерминированный транслятор.
- Tiered namespace policy в `app/services/k8s_guard.py`: production (`prod`/`preprod`/`preupdate`) — read-only, dev (`squad-*`) — write через approval, system/auth (`kube-*`, `mcp`) — forbidden.
- Whitelist по verb/resource; deep-inspection body на `privileged`/`hostNetwork`.
- Потенциально опасные действия проходят через approval flow.

## sre-ai-copilot vs froggy-sre

Оба запускают один и тот же 5-этапный пайплайн. Выбор зависит от контекста:

| | sre-ai-copilot | [froggy-sre](https://github.com/froggychips/froggy-sre) |
|---|---|---|
| **Триггер** | AlertManager webhook (headless) | MCP tool call из Claude Code |
| **Runtime** | Любой сервер / k8s pod | macOS dev-машина |
| **LLM** | Anthropic API | Froggy local → Anthropic fallback |
| **k8s контекст** | In-cluster kubernetes SDK | `kubectl` через kubeconfig |
| **Хранилище** | SQLite + Celery queue | `~/.froggy-sre/incidents/` (local JSON) |
| **Уведомления** | Discord webhook | Ответ в Claude Code |
| **Когда использовать** | Нужен постоянный headless алертинг в production | Анализ инцидентов интерактивно через Claude Code |

## Документация
- [Architecture](docs/ARCHITECTURE.md)
- [Module Docs](docs/MODULE_DOCS.md)
- [Semantic Contract](docs/SEMANTIC_CONTRACT.md)
- [DR Plan](docs/DR.md)

