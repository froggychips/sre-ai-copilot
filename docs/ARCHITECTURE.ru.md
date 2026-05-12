# SRE AI Copilot — Архитектура

## 1. Обзор системы

Приложение состоит из: HTTP API (FastAPI), фоновых задач (Celery), слоя данных (PostgreSQL), внешних MCP-серверов (интеграции), вспомогательной инфраструктуры (Redis, Discord, Kubernetes) и детерминированного движка диагностики, который питает многогипотезный LLM-пайплайн.

## 2. Компоненты времени выполнения

- **API app (`app.main`)**: маршрутизация, auth dependency, middleware метрик, эндпоинты health/readiness.
- **Webhook pipeline (`app.api.webhooks` + `app.workers.tasks`)**: принимает payload AlertManager, строит контекст, запускает агентный пайплайн.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: фоновый разговорный анализ с петлёй по порогу конфиденса.
- **DiagnosticsEngine (`app.diagnostics.engine`)**: правила → типизированный `FactStore`.
- **FactStore (`app.diagnostics.facts`)**: канонический источник правды об инциденте; поддерживает детекцию конфликтов и сериализацию в промпт.
- **MultiHypothesisAgent (`app.agents.multi_hypothesis`)**: fan-out по 4 перспективам (app/infra/deps/runtime) с фильтром по предусловиям.
- **FactCriticAgent (`app.agents.fact_critic`)**: adversarial grounding — отсекает гипотезы, противоречащие наблюдаемым фактам.
- **FixAgent (`app.agents.fix`)**: генерирует структурированный `ExecutionIntent`; учитывает рецидивы и Jira-контекст.
- **SimilarIncidentEngine (`app.core.intelligence.similar_incidents`)**: KG-детекция рецидивов (окно 7 дней).
- **JiraClient (`app.context.jira_client`)**: обогащение через Atlassian REST API для контекста FixAgent.
- **Data layer (`app.database`, `app.repository`)**: SQLAlchemy-модели и CRUD-операции.
- **Integration layer (`app.services.mcp_client`)**: MCP-клиент для k8s, TeamCity и других внешних инструментов.
- **Safety services**: менеджер аппрувов, K8s guard, execution DSL.

## 3. Поток данных (Webhook-инцидент)

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

- На критических задачах настроены Celery retries.
- `/readyz` проверяет доступность PostgreSQL запросом `SELECT 1`.
- Prometheus-метрики по латенси собираются middleware.
- OpenTelemetry-трейсинг инициализируется при старте (экспорт в Tempo при наличии).
- Структурированный audit log через `structlog` (stdout для production, файл для local dev).

## 8. Безопасность

- JWT-based dependency для `/copilot`.
- HMAC-валидация через `ALERTMANAGER_WEBHOOK_SECRET` для webhook-эндпоинтов.
- Guardrails на уровне DSL (`ExecutionIntent`) и валидатора k8s-политик (`K8sSecurityGuard`).
- Approval API для human-in-the-loop перед любым write-действием.
- `SAFE_MODE=true` принудительно в production (config validator бросает исключение при `SAFE_MODE=false` + `ENV=production`).
- Защита от prompt injection (`prompt_guard.detect_injection`) с лимитом на длину входа (`PROMPT_INPUT_MAX_CHARS`).

## 9. Внешние интеграции

| Интеграция | Назначение | Config-ключи |
|---|---|---|
| Kubernetes (MCP) | Состояние подов, логи, события, управление деплоем | через wo-tools MCP |
| VictoriaMetrics | Метрики памяти/CPU за N минут до инцидента | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Контекст последних деплоев | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Известные открытые/закрытые тикеты по сервису | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Approval flow + отчёт об инциденте | `DISCORD_WEBHOOK_URL` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
