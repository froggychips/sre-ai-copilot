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
- **Knowledge Graph (`app.knowledge_graph.*`)**: автоматически наполняемый направленный граф в Postgres (5 таблиц: `kg_services`, `kg_service_edges`, `kg_deployments`, `kg_alerts`, `kg_pod_events`); 5 источников sync (env-vars, NATS env, DSN из secret-key, k8s events, k8s ingresses); confidence scoring с multi-source provenance.
- **Alert enrichment (`app.services.alert_enrichment`)**: deterministic KG-обогащение для `/webhooks/alertmanager/enrich-and-forward` — работает без LLM, ~5 SQL-запросов, собирает `EnrichedContext` (recent_deploys, upstream_alerts, outgoing_deps, pod_events, jira_issues, primary_hypothesis, why_this_matters).
- **Data layer (`app.database`, `app.repository`)**: SQLAlchemy-модели и CRUD-операции.
- **Integration layer (`app.services.mcp_client`)**: MCP-клиент для k8s, TeamCity и других внешних инструментов.
- **Safety services**: менеджер аппрувов, K8s guard, execution DSL.

### Beat tasks (Celery, периодические)

| Task | Расписание | Назначение |
|---|---|---|
| `kg_topology_sync` | каждый час @ :00 | k8s deployments → `kg_services` + рёбра (calls/uses_nats/uses_db) |
| `tc_deploys_to_kg` | каждые 15 мин | TC builds → `kg_deployments` (multi-project, SUCCESS+FAILURE) |
| `k8s_pod_events_sync` | каждые 10 мин | k8s Warning events → `kg_pod_events` |
| `kg_ingress_sync` | каждый час @ :37 | k8s Ingresses → external entrypoint рёбра |
| `kg_alerts_resolve_sync` | каждые 15 мин | refresh `kg_alerts.resolved_at` из AM API |
| `kg_drift_cleanup` | каждый час @ :17 | services из несуществующих namespaces → `synthetic=true` |
| `daily_stats_digest` | раз в день | KG-summary digest в Discord #stats |
| `chronic_alerts_digest` | каждые 6 ч | digest по chronic-suppressed alerts (видимость) |
| `kg_metrics_sync` | каждые 5 мин | VictoriaMetrics PromQL → `kg_service_health` (cpu_pct, mem_pct, restarts_rate, http_5xx_rate, p95_latency_ms) |
| `kg_cluster_health_sync` | каждые 5 мин | снэпшот k8s node-уровня → `kg_cluster_observations` |
| `kg_ingress_observations_sync` | каждые 5 мин | ingress-controller PromQL → `kg_ingress_observations` (схема готова; scrape config для WO ns не настроен — http_5xx/p95 пока 0) |
| `kg_anomaly_detect` | каждые 5 мин | rolling robust-z → `kg_anomaly_observations` |
| `kg_signal_aggregates` | каждые 10 мин | 24h roll-up по anomalies/alerts/deploys/pod_events → `kg_signal_aggregates` |
| `kg_seq_logs_sync` | каждые 10 мин | Seq REST API → `kg_log_observations` (per service × level) |
| `kg_deploy_correlator` | каждые 15 мин | recent incidents × deploys → multi-factor confidence + verdict |
| `team_digest` | ежедневно @ 09:00 UTC | per-team дайджест fragile-сервисов |
| `kg_self_health_check` | каждые 30 мин | 6 canary'ев по KG data quality |

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

## 3a. KG-only Enrichment Flow (без LLM)

Параллельный webhook-путь для deterministic-обогащения алертов без LLM. Сейчас это основной канал вывода в Discord — полный LLM-пайплайн выше гейтнут через `LLM_PIPELINE_ENABLED=False` по умолчанию.

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
     → decide_send() (chronic suppress / rollout-silent)
     → DiscordService.send_enriched_alert(contexts, env, resurfaced)
        → severity-aware embed
        → confidence badges (●●●/●●○/●○○) с provenance
        → кликабельные TC build links + deployer name
```

Latency budget: <500ms p95 synchronous в HTTP handler. 0 LLM-токенов. Стоимость на alert: 0.

## 3b. Active Observability Layer (Wave 1–5)

Поверх дискретных таблиц событий (deployments / alerts / pod_events) KG также материализует непрерывный time-series слой по каждому сервису — он питает дайджест, anomaly-детектор, deploy correlator и Discord pipeline.

### Time-series materialization (Wave 1)

| Таблица | Источник | Гранулярность |
|---|---|---|
| `kg_service_health` | VictoriaMetrics PromQL через `metrics_sync.py` | 5 мин, на сервис |
| `kg_cluster_observations` | k8s node API через `cluster_health_sync.py` | 5 мин, на ноду |
| `kg_ingress_observations` | ingress-controller PromQL через `ingress_observations_sync.py` | 5 мин, на host |
| `kg_signal_aggregates` | 24-h roll-up через `signal_aggregates.py` | hourly refresh, на сервис |

Каждая запись из PromQL оборачивается в Postgres `SAVEPOINT`; `IntegrityError` на UNIQUE-ключе (например, дубль `(service_id, ts)`) откатывает savepoint и продолжает цикл. Поэтому sync естественно идемпотентен на пересекающихся окнах.

Wave 5 PromQL gotcha (фиксируем для будущих авторов): `mem_pct` нужно считать как `avg(rate(container_memory_working_set_bytes) / on(pod) kube_pod_container_resource_limits)` — сначала делим, потом агрегируем. Форма «aggregate-then-divide» несколько дней молча возвращала нули, потому что many-to-one join схлопывал результат до деления. Canary `kg_self_health` (ниже) добавлен специально, чтобы такие тихие деградации детектились автоматически.

Честное ограничение: ingress observations материализуются в схему, но scrape config, который выставлял бы `nginx_ingress_controller_*` для WO namespace'ов, не настроен — поэтому `http_5xx_rate` и `p95_latency_ms` сейчас 0 у всех сервисов. Pipeline деградирует gracefully: anomaly detection на плоском нуле просто не выдаёт observations.

### Anomaly detection (Wave 2 + Wave 6 update)

`app/knowledge_graph/anomaly_detection.py` сканирует материализованный time-series и пишет `kg_anomaly_observations`.

- **Robust-z статистика.** `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Median + MAD вместо mean + stddev — одиночный выброс в baseline не отравляет порог.
- **Seasonal baseline.** При ≥50 исторических точках baseline стратифицируется по hour-of-day, чтобы поглотить дневной паттерн. `extras.method = robust_z_seasonal`. Ниже порога — flat baseline, `robust_z_flat`.
- **Configurable thresholds** через `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) и `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`).
- **Volume guard.** Не более 3 observations на (service, metric) в час — защита от flood'а при затяжной аномалии.
- **Baseline window:** rolling 7 дней.

### Deploy ↔ Incident correlator (Wave 2 + Wave 6 update)

`app/rca/deploy_correlator.py` вызывается inline из `IncidentPipeline._enrich_deploy_correlation` для каждого инцидента, дошедшего до enrichment.

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

`app/services/team_digest.py` запускается ежедневно в 09:00 UTC и собирает per-team Discord embed:

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

- `DISCORD_APPROVERS_USER_IDS` — CSV-список Discord user ID
- `DISCORD_APPROVERS_ROLE_IDS` — CSV-список Discord role ID (member должен иметь хотя бы одну)
- `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` — квота на пользователя, default 5

Семантика:

- **Fail-closed** когда оба списка пустые — кнопка отказывает, audit `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.
- Неавторизованный клик получает ephemeral deny и audit `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
- Rate-limit in-memory. Authz-fail НЕ потребляет квоту — отказанного пользователя нельзя использовать для исчерпания лимита легитимного approver'а.

### KG self-health canary

`app/knowledge_graph/self_health.py` — beat-задача "monitoring of the monitoring", добавлена после Wave 5 mem_pct silent-failure, чтобы такой класс регрессий ловился автоматом.

Запускается раз в 30 минут. Шесть проверок, каждая возвращает `ok` / `warn` / `fail`:

1. **`materialization_zero_rate`** — % строк в `kg_service_health` где метрика = 0/NULL за 24 ч. Allowlist для known-zero метрик (`http_5xx_rate`, `p95_latency_ms`, пока scrape config не на месте).
2. **`sync_lag`** — `max(ts)` на beat-задачу vs ожидаемый интервал; >2× → warn, >5× → fail.
3. **`anomaly_signal_health`** — count `kg_anomaly_observations` за 24 ч. 0 → warn (flat baseline или детектор сломан). >500 → warn (порог слишком слабый, overload).
4. **`alerts_resolve_freshness`** — count `kg_alerts` с `fired_at < 7d ago` и `resolved_at IS NULL`. >20 → warn.
5. **`pod_events_link_rate`** — % `kg_pod_events` за 24 ч с `service_id NOT NULL`. <80% warn, <50% fail (StS resolver regression).
6. **`edges_freshness`** — % `kg_service_edges` с `last_seen_at < 24h` или NULL. >30% stale → warn (kg_topology_sync regression).

Вывод: строка в audit-log за прогон; на любой `fail`/`warn` — single Discord embed в `DISCORD_WEBHOOK_SELF_HEALTH_URL` (отделено от `#infra-error`, чтобы не топить операционные алерты). 6-часовое dedup-окно не даёт одному и тому же failing-canary заспамить канал.

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
- **PII redaction** (`app/services/pii_redaction.py`) — write-time scrubbing log-сэмплов Seq и defense-in-depth scrubbing строк Discord embed. Паттерны: email, IPv4/IPv6, JWT, bearer, UUID, длинный hex, `password|token|secret|api_key` key/value.
- **Approve / Decline authz** на Discord-кнопках — allowlists `DISCORD_APPROVERS_USER_IDS` / `DISCORD_APPROVERS_ROLE_IDS`, квота `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR`. Fail-closed когда оба списка пустые.
- **Выделенная read-only Postgres-роль** (`kg_reader`) для внешнего доступа к KG — отдельный user, `SELECT` только на `kg_*` и `alembic_version`, no default privileges (будущие таблицы нужно грантить руками).

## 9. Внешние интеграции

| Интеграция | Назначение | Config-ключи |
|---|---|---|
| Kubernetes (MCP) | Состояние подов, логи, события, управление деплоем | через wo-tools MCP |
| VictoriaMetrics | Метрики памяти/CPU за N минут до инцидента | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Контекст последних деплоев | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Известные открытые/закрытые тикеты по сервису | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Approval flow + отчёт об инциденте | `DISCORD_WEBHOOK_URL` |
| Discord (per-team) | Per-team channel routing для digests/incidents | `DISCORD_TEAM_CHANNEL_MAP` (JSON) |
| Discord (self-health) | KG canary алерты, отдельно от `#infra-error` | `DISCORD_WEBHOOK_SELF_HEALTH_URL` |
| Discord (authz) | Allowlist для Approve/Decline кнопок | `DISCORD_APPROVERS_USER_IDS`, `DISCORD_APPROVERS_ROLE_IDS`, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` |
| Seq | Поток application-логов → `kg_log_observations` | `SEQ_URL`, `SEQ_API_KEY` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
| Anomaly tuning | Robust-z пороги | `KG_ANOMALY_ROBUST_Z_WARN`, `KG_ANOMALY_ROBUST_Z_CRIT` |
