# SRE AI Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688)](https://fastapi.tiangolo.com/)
[![Celery](https://img.shields.io/badge/Celery-5.3-37814A)](https://docs.celeryq.dev/)
[![Release](https://img.shields.io/badge/release-v0.12.0-blue)](CHANGELOG.md)

> **[English](#english) · [Русский](#русский)**

---

<a name="english"></a>
## English

**SRE AI Copilot** is a backend service that turns Prometheus AlertManager webhooks into either an **advisory analysis** (default) or an **auto-remediation with human approval** (opt-in) of a Kubernetes incident — root cause + recommended actions, posted as a Discord embed.

> **Default = advisory mode.** With out-of-the-box settings (`EXECUTOR_ENABLED=false`, `EXECUTOR_APPROVAL_ENABLED=false`) the copilot does **not** call `kubectl` — it analyzes, posts to Discord, and stops. Engineer acts manually.
>
> **Opt-in = auto-remediator with approval.** Setting `EXECUTOR_ENABLED=true` adds a 9th pipeline stage that validates the proposed action against `kube-apiserver` via `kubectl ... --dry-run=server`. Setting `EXECUTOR_APPROVAL_ENABLED=true` additionally shows an «⚙️ Apply» button on the Discord embed (only when dry-run passed and risk ≤ medium); two-click confirmation → real `kubectl` under `K8sSecurityGuard` with full OTEL audit. See [Roadmap → Execution](#roadmap--execution) below for the ramp-up plan.

### What's new

- **v0.12.0 — Wave 8 (KG Metadata + UX Polish)**: k8s Jobs/CronJobs coverage
  (`kg_k8s_jobs`, `runs_as_job` edge), PVC/PV storage subgraph (`kg_storage_volumes`,
  `uses_volume` + `bound_to` edges in `kg_volume_edges`), multi-signal owner
  inference (prefix + deploy-history + labels + manual override), `stale_class`
  column on `kg_services` (active/expected_stale/suspicious_stale), formal
  KG schema/quality contract v2.2 (`app/knowledge_graph/contract.py` +
  `docs/KG_SCHEMA_CONTRACT.md`), Discord embed UX polish (PATCH-dedup,
  human-time, pod trail), stats digest UX overhaul (trends, unowned action
  block, blast-radius rename), `quality_report` CLI + 7-case snapshot fixtures
  gallery for UX regression-guard.
- **v0.11.0 — Wave 7 (Topology Expansion)**: PodEvent ↔ ServiceEdge runtime
  correlation (cheap OTEL-substitute, confirms existing edges), declarative
  k8s Service + Ingress parser (new `serves_traffic` + `routes_to` edges),
  NATS subjects parser from monorepo source (subject-level pub/sub direction
  on `uses_nats` edges).
- **v0.9.0–v0.10.0 — Active observability layer (Wave 1–6)**: VictoriaMetrics
  time-series materialization (`kg_service_health`), anomaly detection
  (robust-z + seasonal baseline), deploy ↔ incident correlator, Seq logs
  integration, daily team digest, Discord pipeline overhaul (dedup, severity
  routing, per-team channels), PII redaction, Approve/Decline authz, KG
  self-health canary.

### What it does

- Receives AlertManager webhooks (`POST /webhooks/alertmanager`).
- **Fingerprint deduplication**: skips re-running the pipeline for alerts already in-flight (OPEN → RESOLVED). Only FAILED incidents are retried.
- **Flapping detection**: if an alert fires after RESOLVED, increments `flap_count` and re-runs the pipeline with explicit context — "this alert has cycled N times; RESOLVED was likely premature."
- Runs `DiagnosticsEngine` — deterministic rules produce a typed `FactStore` (OOM killed, process crash, crashloop, …) before any LLM call.
- Detects **fact conflicts** (`oom_killed` + `process_crash` both true = contradiction → confidence capped, `<conflicts>` block injected into prompts).
- Runs a **multi-hypothesis fan-out** across 4 perspectives (app / infra / deps / runtime) filtered by `PERSPECTIVE_PRECONDITIONS`, then adversarially grounds each hypothesis against the `FactStore` via `FactCriticAgent`.
- Enriches context with **cluster-wide health snapshot** at incident time: nodes ready, pod failures, crashloops, CPU/mem/disk peak, firing alert counts — same metrics as the `#stats` daily report. Lets the LLM distinguish "isolated pod issue" from "cluster-wide pressure."
- Supports **Node\* alerts** (NodeDiskIOSaturation, NodeMemoryWillExhaustSoon, …): `instance`/`node` labels are used for enrichment and displayed in the Discord embed instead of `pod`.
- Enriches context from **Atlassian Jira** (open/resolved tickets for the service), **TeamCity** (recent deploys), and **VictoriaMetrics** (memory/CPU window per pod + cluster health).
- Detects **recurrence**: same service resolved < 7 days → `FixAgent` switches to investigative mode (no restart recommendations).
- Posts a **single Discord embed** per incident (title + root cause + synthesis + feedback buttons), replacing the previous two-message flow.
- **👍 / 👎 feedback buttons** on every embed: 👍 saves immediately; 👎 requires a two-step confirmation ("Confirm: was the model's *analysis* wrong?") to prevent accidental negative feedback. Stored in `IncidentRecord.user_feedback`.
- **Structured `ExecutionIntent`** alongside prose: `FixAgent` emits JSON in the `ExecutionIntent` schema (`action`, `resource_type`, `resource_name`, `namespace`, `params`, `risk`); pydantic-validated, `FORBIDDEN_NAMESPACES` rejected at parse time, persisted to `IncidentRecord.analysis.execution_intent`.
- **Executor stage** (opt-in via `EXECUTOR_ENABLED=true`): server-side dry-run of the intent (`kubectl ... --dry-run=server`) under `K8sSecurityGuard`; result captured in `executor_result` and shown as a "Dry-run verdict" field on the Discord embed.
- **Discord Apply button** (opt-in via `EXECUTOR_APPROVAL_ENABLED=true`): two-click confirmation on the embed → invokes `K8sService.execute_intent(intent, dry_run=False, post_approval=True)`; idempotency by `incident_id`, eligible only when dry-run passed and risk ∈ {low, medium}; HIGH-risk and ineligible incidents never get the button.
- Full **OTEL audit trail**: `sre.copilot.incident.process` root span, per-stage child spans, `execution_intent_parsed` / `executor_status` attributes, `guardrail.blocked` events emitted when the guard rejects an operation, `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED` audit events.

### Tech stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Queue | Celery + Redis |
| Database | PostgreSQL + SQLAlchemy (SQLite for local dev) |
| LLM | Anthropic Claude (API key or `claude --print` CLI subprocess) |
| Observability | Prometheus, OpenTelemetry → Tempo, structlog |
| Integrations | Discord, Kubernetes, Jira, TeamCity (MCP), VictoriaMetrics |
| Deploy | Helm chart (`helm/sre-ai-copilot/`) + k8s raw manifests (`k8s/`) |

### Quick start

```bash
# 1. Clone and create .env (copy from example)
cp .env.example .env   # fill in required values (see below)

# 2. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run (local, no containers)
uvicorn app.main:app --reload --port 8000

# 4. Or with Docker Compose
docker-compose up -d
```

**Minimum `.env` for local dev (claude CLI backend, no API key):**

```env
DATABASE_URL=sqlite:///./sre_copilot.db
REDIS_URL=redis://localhost:6379/0
LLM_BACKEND=claude_cli
SAFE_MODE=true
APPROVAL_REQUIRED=true
DISCORD_DRY_RUN=true
PIPELINE_DIRECT_INVOKE=true
```

**Full `.env` reference:**

| Key | Purpose | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | Required when `LLM_BACKEND=anthropic` | prod |
| `DISCORD_WEBHOOK_URL` | Incident embed + approval notifications | prod |
| `DISCORD_PUBLIC_KEY` | Ed25519 key for `/discord/interactions` signature verification | for buttons |
| `DISCORD_DRY_RUN` | `true` = log instead of posting to Discord | dev |
| `ALERTMANAGER_WEBHOOK_SECRET` | HMAC-SHA256 webhook auth — mandatory in `ENV=production` | prod |
| `JWT_PUBLIC_KEY` | `/copilot` endpoint auth | prod |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | Jira enrichment | optional |
| `VICTORIA_METRICS_URL` | Pod metrics window + cluster health snapshot | optional |
| `TEAMCITY_MCP_URL` / `TEAMCITY_MCP_TOKEN` | Deploy context via TeamCity MCP | optional |
| `PIPELINE_DIRECT_INVOKE` | Run pipeline inline (skip Celery) — for local e2e | dev |

### Discord integration

The copilot posts a single embed per incident to `DISCORD_WEBHOOK_URL` containing the alert header, root cause, and synthesis. Feedback buttons (👍 / 👎) allow engineers to rate the analysis quality.

**Enabling feedback buttons** requires registering an Interactions Endpoint URL in the Discord Developer Portal:

```
Discord Developer Portal → Application → General Information →
  Interactions Endpoint URL = https://<your-host>/discord/interactions
```

Set `DISCORD_PUBLIC_KEY` (from General Information) in `.env`. For local testing, expose the service with [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
cloudflared tunnel --url http://localhost:8000
```

> **⏳ Experiment running until 2026-05-15**: single embed replaces the two-message flow
> (Spidey Bot raw alert + copilot analysis). After evaluation — readability, missed alerts,
> latency — the routing will be either confirmed or reverted. To activate on the real channel:
> 1. Remove the direct AlertManager → Discord webhook for incident alerts.
> 2. Set `DISCORD_WEBHOOK_URL` in production `.env`.
> 3. Set `DISCORD_DRY_RUN=false`.

### Helm install

```bash
helm install sre-ai-copilot helm/sre-ai-copilot/ \
  --set ingress.host=sre-ai.example.com \
  --set image.tag=0.6.0
```

Fill secrets before installing — see `helm/sre-ai-copilot/templates/secret.yaml`.

### API endpoints

| Endpoint | Description |
|---|---|
| `POST /webhooks/alertmanager` | AlertManager batch webhook (with fingerprint dedup + flapping detection) |
| `GET /webhooks/status/{task_id}` | Celery task status |
| `POST /discord/interactions` | Discord button interactions (Ed25519-verified) |
| `POST /copilot` | Conversational analysis |
| `GET /jobs/{task_id}` | Copilot task status |
| `POST /approvals/{id}/approve\|reject` | Human approval |
| `POST /replay/{incident_id}` | Re-run historical incident |
| `POST /evaluation/{id}/submit` | Feedback submission |
| `GET /healthz`, `GET /readyz` | Liveness / readiness |

### Combat runs (accuracy history)

Full details: [docs/RUNBOOK.md](docs/RUNBOOK.md)

| Run | Incident | Result | Problem found | Fix shipped |
|---|---|---|---|---|
| 1 | Smoke SIGSEGV | ❌ unresolved | `OOMKilledRule` text-regex false positive on other pods' events | Structured gate: target exit ≠ 137 → `observed=False` |
| 2 | Smoke SIGSEGV | ❌ unresolved | `oom_killed` + `process_crash` both True → FactCritic kills all hypotheses | `MUTUALLY_EXCLUSIVE_PAIRS` conflict detection, confidence cap 0.60 |
| 3 | Live notificator exit 139 | ❌ unresolved | Same OOM false positive on real cluster; KG polluted with "Manual triage required" | OOM structured gate deployed; KG quality gate; `_is_quality_cause()` filter |
| 4 | Live notificator exit 139 | ✅ **resolved** | Jira `GET /search` → 410 Gone (graceful degrade) | All fixes active; cause: "Nil pointer dereference in startup initialization path" |
| 5 | Live preprod pod crash | ✅ **resolved** | TC context missing (`no_deploys_or_no_timestamp`) — flagged as gap, not false root cause | Correct cautious behaviour; TC MCP URL not configured locally |
| 6 | Live preprod pod crash | ✅ **resolved** | Pipeline self-diagnosed a false refutation in synthesis | Correct — synthesis explicitly noted the contradiction and recommended manual check |

### Security

- **Defence in depth around kubectl**: AI never calls kubectl directly — `FixAgent` emits structured `ExecutionIntent` (JSON, pydantic-validated, `FORBIDDEN_NAMESPACES` rejected at parse time), the `DSLTranslator` produces the canonical `kubectl` string deterministically, and `K8sSecurityGuard` validates `(verb, resource, namespace)` derived structurally from the action — not from text-parsing the command. Real writes (`dry_run=False`) additionally require `post_approval=True` set only by the Discord approval consumer.
- Tiered namespace policy enforced by `K8sSecurityGuard.validate`: `prod`/`preprod` read-only; `squad-*` write via approval; `kube-*`/`mcp` forbidden.
- `SAFE_MODE=true` enforced in `ENV=production` (config validator raises otherwise) — a real write outside an approved path returns `SAFE_MODE: Manual approval required.`
- AlertManager webhook auth: HMAC-SHA256 on the body (`ALERTMANAGER_WEBHOOK_SECRET` is mandatory in production, the config validator refuses to start without it).
- Prompt injection guard with `PROMPT_INPUT_MAX_CHARS` cap.
- Discord interactions endpoint verifies Ed25519 signature on every request (Discord requirement). Apply button uses two-click confirmation (mirror of the 👎 feedback flow) to prevent accidental writes.
- Full OTEL audit trail and `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED` events — see [docs/AUDIT.md](docs/AUDIT.md).

### Roadmap — Execution

The executor track is **delivered and gated behind explicit opt-in flags** as of v0.7.0. The remaining work is operational, not code.

| # | Step | Status |
|---|---|---|
| 1 | `FixAgent` emits structured `ExecutionIntent` alongside prose | ✅ v0.7.0 (PR #23) |
| 2 | `executor` stage after `risk` with `dry_run=True` + `K8sSecurityGuard.validate` | ✅ v0.7.0 (PR #26) |
| 3 | Discord Apply consumer with two-click confirm → real `kubectl` under guard | ✅ v0.7.0 (PR #27) |
| 4 | End-to-end smoke on non-prod `squad-*` cluster + production ramp-up plan | 🟡 In progress (this PR) |

**Ramp-up plan (post-merge):**

1. **Dev**: `EXECUTOR_ENABLED=true`, `EXECUTOR_APPROVAL_ENABLED=true` on local dev with `DISCORD_DRY_RUN=true` — verify embed buttons appear correctly.
2. **One preprod squad-N namespace**: enable both flags via Helm value override; trigger a synthetic alert (or wait for a real one); click Apply on a low-risk action (`get_logs`, `describe_resource`). Verify `executor_applied` row in DB and audit log.
3. **All preprod-squad-***: gradual rollout one namespace per day, watch for `EXECUTOR_APPLY_REFUSED` rate.
4. **Production-squad-N**: same procedure, `low`-risk only for first week, then `medium` after one clean incident.
5. **No HIGH risk auto-apply ever** — by design. HIGH-risk intents never get the Apply button.

See [docs/RUNBOOK.md → Executor incidents](docs/RUNBOOK.md#executor-incidents) for operational procedures.

### Documentation

| Document | EN | RU |
|---|---|---|
| Architecture | [ARCHITECTURE.md](docs/ARCHITECTURE.md) | [ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md) |
| Runbook / Combat runs | [RUNBOOK.md](docs/RUNBOOK.md) | [RUNBOOK.ru.md](docs/RUNBOOK.ru.md) |
| Module docs | [MODULE_DOCS.md](docs/MODULE_DOCS.md) | [MODULE_DOCS.ru.md](docs/MODULE_DOCS.ru.md) |
| Audit trail (OTEL) | [AUDIT.md](docs/AUDIT.md) | — |
| Semantic contract | [SEMANTIC_CONTRACT.md](docs/SEMANTIC_CONTRACT.md) | — |
| FAQ | [FAQ.md](docs/FAQ.md) | [FAQ.ru.md](docs/FAQ.ru.md) |
| DR plan | [DR.md](docs/DR.md) | — |
| Changelog | [CHANGELOG.md](CHANGELOG.md) | — |

### sre-ai-copilot vs froggy-sre

| | sre-ai-copilot | [froggy-sre](https://github.com/froggychips/froggy-sre) |
|---|---|---|
| **Trigger** | AlertManager webhook (headless) | MCP tool call from Claude Code |
| **Runtime** | Any server / k8s pod | macOS dev machine |
| **LLM** | Anthropic API | Froggy local → Anthropic fallback |
| **k8s context** | In-cluster Kubernetes SDK | `kubectl` via kubeconfig |
| **Storage** | PostgreSQL + Celery queue | `~/.froggy-sre/incidents/` (local JSON) |
| **Notifications** | Discord webhook | Reply in Claude Code |
| **When to use** | Persistent headless alerting in production | Interactive incident analysis via Claude Code |

---

<a name="русский"></a>
## Русский

**SRE AI Copilot** — backend-сервис, который превращает webhook-и Prometheus AlertManager либо в **аналитическую справку** (по дефолту), либо в **авто-ремедиатор с human approval** (opt-in) по инциденту в Kubernetes: root cause + рекомендуемые действия, постит embed-ом в Discord.

> **Default = advisory-режим.** Со стандартными настройками (`EXECUTOR_ENABLED=false`, `EXECUTOR_APPROVAL_ENABLED=false`) copilot **не** вызывает `kubectl` — анализирует, постит в Discord, останавливается. Инженер действует руками.
>
> **Opt-in = auto-remediator с approval.** При `EXECUTOR_ENABLED=true` добавляется 9-я стадия пайплайна: предложенное действие валидируется через `kube-apiserver` командой `kubectl ... --dry-run=server`. При `EXECUTOR_APPROVAL_ENABLED=true` на embed дополнительно появляется кнопка «⚙️ Apply» (только если dry-run прошёл и risk ≤ medium); двухшаговое подтверждение → реальный `kubectl` под `K8sSecurityGuard` + полный OTEL audit. См. [Roadmap — Execution](#roadmap--execution-1) ниже для плана выкатки.

### Что нового

- **v0.12.0 — Wave 8 (KG Metadata + UX Polish)**: покрытие k8s Jobs/CronJobs
  (`kg_k8s_jobs`, edge `runs_as_job`), storage-подграф PVC/PV
  (`kg_storage_volumes`, edges `uses_volume` + `bound_to` в `kg_volume_edges`),
  multi-signal owner inference (prefix + deploy-history + labels + manual
  override), column `stale_class` в `kg_services` (active/expected_stale/
  suspicious_stale), формализованный KG schema/quality contract v2.2
  (`app/knowledge_graph/contract.py` + `docs/KG_SCHEMA_CONTRACT.md`), polish
  Discord embed (PATCH-dedup, human-time, pod trail), переработка stats
  digest UX (trends, unowned action block, blast-radius rename), CLI
  `quality_report` + 7 snapshot-фикстур для UX regression-guard.
- **v0.11.0 — Wave 7 (Topology Expansion)**: runtime correlation
  PodEvent ↔ ServiceEdge (дешёвый OTEL-substitute, подтверждает существующие
  edges), declarative-парсер k8s Service + Ingress (новые edges
  `serves_traffic` + `routes_to`), парсер NATS subjects из monorepo
  (subject-level direction pub/sub на edges `uses_nats`).
- **v0.9.0–v0.10.0 — Active observability layer (Wave 1–6)**: time-series
  материализация VictoriaMetrics (`kg_service_health`), детекция аномалий
  (robust-z + seasonal baseline), deploy ↔ incident correlator, интеграция
  Seq, daily team digest, переработка Discord-пайплайна (dedup, severity
  routing, per-team каналы), PII redaction, authz на Approve/Decline,
  self-health canary KG.

### Что умеет

- Принимает алерты AlertManager (`POST /webhooks/alertmanager`).
- **Дедупликация по fingerprint**: повторные алерты для инцидента в статусе OPEN→RESOLVED пропускаются. Повторный запуск только для FAILED.
- **Детекция флаппинга**: если алерт срабатывает после RESOLVED — инкрементирует `flap_count` и перезапускает пайплайн с явным контекстом «этот алерт уже циклировал N раз; RESOLVED между срабатываниями, вероятно, был ложным».
- Запускает `DiagnosticsEngine` — детерминированные правила выдают типизированный `FactStore` (oom_killed, process_crash, crashloop, …) до любого LLM-вызова.
- Детектирует **факт-конфликты** (`oom_killed` + `process_crash` одновременно True = противоречие → cap конфиденса, блок `<conflicts>` в промпт).
- Запускает **многогипотезный fan-out** по 4 перспективам (app / infra / deps / runtime) с фильтром `PERSPECTIVE_PRECONDITIONS`, затем adversarially проверяет каждую гипотезу через `FactCriticAgent`.
- Обогащает контекст **snapshot кластерного здоровья** в момент инцидента: ноды ready, упавшие поды, crashloop-ы, CPU/mem/disk peak, счётчики firing alerts — те же метрики, что в ежедневном отчёте `#stats`. Позволяет LLM различать «изолированный pod» и «кластерное давление».
- Поддерживает **Node\*-алерты** (NodeDiskIOSaturation, NodeMemoryWillExhaustSoon, …): labels `instance`/`node` используются для обогащения и отображаются в Discord вместо `pod`.
- Обогащает контекст из **Atlassian Jira** (тикеты по сервису), **TeamCity** (последние деплои), **VictoriaMetrics** (память/CPU пода + кластерный snapshot).
- Детектирует **рецидивы**: тот же сервис resolved < 7 дней → `FixAgent` переключается в investigative-режим (не рекомендует рестарт).
- Постит **один Discord embed** на инцидент (заголовок алерта + root cause + синтез + кнопки фидбека), заменяя прежние два сообщения.
- **Кнопки 👍 / 👎** на каждом embed: 👍 сохраняется сразу; 👎 требует двухшагового подтверждения («Подтверди: выводы модели были ошибочными?») — защита от случайного клика. Результат сохраняется в `IncidentRecord.user_feedback`.
- **Структурный `ExecutionIntent`** рядом с prose: `FixAgent` выдаёт JSON по схеме `ExecutionIntent` (`action`, `resource_type`, `resource_name`, `namespace`, `params`, `risk`); pydantic-валидация, `FORBIDDEN_NAMESPACES` отбрасываются на парсе, сохраняется в `IncidentRecord.analysis.execution_intent`.
- **Executor-стадия** (opt-in `EXECUTOR_ENABLED=true`): server-side dry-run intent-а (`kubectl ... --dry-run=server`) под `K8sSecurityGuard`; результат в `executor_result` и на Discord-embed полем «Dry-run verdict».
- **Discord Apply-кнопка** (opt-in `EXECUTOR_APPROVAL_ENABLED=true`): двухшаговое подтверждение на embed → `K8sService.execute_intent(intent, dry_run=False, post_approval=True)`; идемпотентность по `incident_id`, eligible только при dry-run ok и risk ∈ {low, medium}; HIGH-risk и ineligible инциденты кнопку не получают вообще.
- Полный **OTEL audit trail**: root span `sre.copilot.incident.process`, child-спан на стадию, атрибуты `execution_intent_parsed` / `executor_status`, events `guardrail.blocked` при отказе guard-а, audit-события `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED`.

### Быстрый старт

```bash
# 1. Клонировать и настроить .env
cp .env.example .env   # заполнить нужные поля

# 2. Зависимости
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Запуск локально (без контейнеров)
uvicorn app.main:app --reload --port 8000

# 4. Или через Docker Compose
docker-compose up -d
```

**Минимальный `.env` для local dev (без API key):**

```env
DATABASE_URL=sqlite:///./sre_copilot.db
REDIS_URL=redis://localhost:6379/0
LLM_BACKEND=claude_cli
SAFE_MODE=true
APPROVAL_REQUIRED=true
DISCORD_DRY_RUN=true
PIPELINE_DIRECT_INVOKE=true
```

> **LLM_BACKEND=claude_cli** — subprocess-обёртка вокруг `claude --print`.
> Полный пайплайн без Anthropic API key. Для production: `LLM_BACKEND=anthropic` + `ANTHROPIC_API_KEY`.

**Все переменные окружения:**

| Ключ | Назначение | Обязательность |
|---|---|---|
| `ANTHROPIC_API_KEY` | При `LLM_BACKEND=anthropic` | prod |
| `DISCORD_WEBHOOK_URL` | Embed-отчёты + approval | prod |
| `DISCORD_PUBLIC_KEY` | Ed25519-ключ для верификации `/discord/interactions` | для кнопок |
| `DISCORD_DRY_RUN` | `true` = логировать вместо отправки | dev |
| `ALERTMANAGER_WEBHOOK_SECRET` | HMAC-SHA256 аутентификация вебхука | prod |
| `JWT_PUBLIC_KEY` | Аутентификация `/copilot` | prod |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | Обогащение из Jira | опционально |
| `VICTORIA_METRICS_URL` | Метрики пода + кластерный snapshot | опционально |
| `TEAMCITY_MCP_URL` / `TEAMCITY_MCP_TOKEN` | Контекст деплоев через TeamCity MCP | опционально |
| `PIPELINE_DIRECT_INVOKE` | Запуск пайплайна inline без Celery | dev |

### Discord-интеграция

Copilot постит один embed на инцидент: заголовок алерта (alertname · namespace), root cause, синтез и кнопки фидбека. Кнопки требуют регистрации Interactions Endpoint:

```
Discord Developer Portal → Application → General Information →
  Interactions Endpoint URL = https://<your-host>/discord/interactions
```

Установить `DISCORD_PUBLIC_KEY` (из General Information) в `.env`. Для локального теста — пробросить порт через [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
cloudflared tunnel --url http://localhost:8000
```

> **⏳ Эксперимент до 2026-05-15**: один embed заменяет два сообщения (сырой алерт от
> Spidey Bot + анализ copilot). После оценки (читаемость, пропущенные алерты, latency) —
> подтверждение или откат. Для активации на боевом канале:
> 1. Убрать прямой Alertmanager → Discord webhook для инцидентных алертов.
> 2. Прописать `DISCORD_WEBHOOK_URL` в production `.env`.
> 3. Установить `DISCORD_DRY_RUN=false`.

### Helm

```bash
helm install sre-ai-copilot helm/sre-ai-copilot/ \
  --set ingress.host=sre-ai.example.com \
  --set image.tag=0.6.0
```

Перед установкой заполнить секреты — см. `helm/sre-ai-copilot/templates/secret.yaml`.

### API endpoints

| Endpoint | Описание |
|---|---|
| `POST /webhooks/alertmanager` | AlertManager webhook (с dedup + флаппинг-детекцией) |
| `GET /webhooks/status/{task_id}` | Статус Celery-задачи |
| `POST /discord/interactions` | Discord-взаимодействия с кнопками (Ed25519-верификация) |
| `POST /copilot` | Разговорный анализ |
| `GET /jobs/{task_id}` | Статус copilot-задачи |
| `POST /approvals/{id}/approve\|reject` | Human approval |
| `POST /replay/{incident_id}` | Перезапуск исторического инцидента |
| `POST /evaluation/{id}/submit` | Ручная отправка фидбека |
| `GET /healthz`, `GET /readyz` | Liveness / readiness |

### Боевые прогоны (история точности)

Подробно: [docs/RUNBOOK.ru.md](docs/RUNBOOK.ru.md)

| Прогон | Инцидент | Результат | Найденная проблема | Задеплоенный фикс |
|---|---|---|---|---|
| 1 | Smoke SIGSEGV | ❌ unresolved | `OOMKilledRule` text-regex срабатывал на события других подов | Структурный шлюз: exit ≠ 137 → `observed=False` |
| 2 | Smoke SIGSEGV | ❌ unresolved | `oom_killed` + `process_crash` оба True → FactCritic убивает все гипотезы | `MUTUALLY_EXCLUSIVE_PAIRS`, cap конфиденса до 0.60 |
| 3 | Live exit 139 | ❌ unresolved | Тот же false positive на реальном кластере; KG загрязнён | Структурный шлюз OOM + KG quality gate |
| 4 | Live exit 139 | ✅ **resolved** | Jira 410 Gone (graceful degrade) | Все фиксы активны; причина: "Nil pointer dereference…" |
| 5 | Live preprod pod crash | ✅ **resolved** | TC-контекст missing (`no_deploys_or_no_timestamp`) — отмечен как gap, не ложный root cause | Корректная осторожность; `TEAMCITY_MCP_URL` не настроен локально |
| 6 | Live preprod pod crash | ✅ **resolved** | Пайплайн самодиагностировал ложное опровержение в синтезе | Корректно — синтез явно отметил противоречие и рекомендовал ручную проверку |

### Безопасность

- **Defence in depth вокруг kubectl**: AI не вызывает kubectl напрямую — `FixAgent` выдаёт структурный `ExecutionIntent` (JSON, pydantic-валидирован, `FORBIDDEN_NAMESPACES` отбрасываются на парсе), `DSLTranslator` детерминированно строит kubectl-строку, `K8sSecurityGuard` валидирует `(verb, resource, namespace)` структурно (не через text-parsing). Реальный write (`dry_run=False`) дополнительно требует `post_approval=True`, который ставится только из Discord approval consumer-а.
- Tiered namespace policy в `K8sSecurityGuard.validate`: `prod`/`preprod` — read-only; `squad-*` — write через approval; `kube-*`/`mcp` — forbidden.
- `SAFE_MODE=true` принудительно в `ENV=production` (config-validator валит старт иначе) — реальный write вне утверждённого пути возвращает `SAFE_MODE: Manual approval required.`
- AlertManager-webhook аутентификация: HMAC-SHA256 на body (`ALERTMANAGER_WEBHOOK_SECRET` обязателен в production, без него config-validator не даёт стартовать).
- Защита от prompt injection с лимитом `PROMPT_INPUT_MAX_CHARS`.
- Discord Interactions endpoint верифицирует Ed25519-подпись на каждом запросе (требование Discord). Кнопка Apply имеет двухшаговое подтверждение (паттерн зеркал 👎) — защита от случайных кликов.
- Полный OTEL audit trail + события `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED` — см. [docs/AUDIT.md](docs/AUDIT.md).

### Roadmap — Execution

Executor-трек **сделан и закрыт за явные opt-in флаги** в v0.7.0. Оставшаяся работа — операционная, не кодовая.

| # | Шаг | Статус |
|---|---|---|
| 1 | `FixAgent` отдаёт структурный `ExecutionIntent` рядом с prose | ✅ v0.7.0 (PR #23) |
| 2 | `executor`-стадия после `risk` с `dry_run=True` + `K8sSecurityGuard.validate` | ✅ v0.7.0 (PR #26) |
| 3 | Discord Apply consumer с двухшаговым confirm → реальный `kubectl` под guard | ✅ v0.7.0 (PR #27) |
| 4 | End-to-end smoke на non-prod `squad-*` кластере + production ramp-up план | 🟡 In progress (этот PR) |

**Ramp-up план (после merge):**

1. **Dev**: `EXECUTOR_ENABLED=true`, `EXECUTOR_APPROVAL_ENABLED=true` локально с `DISCORD_DRY_RUN=true` — убедиться что кнопки появляются на embed-е корректно.
2. **Один preprod squad-N namespace**: включить оба флага через Helm value override; спровоцировать синтетический алерт (или дождаться реального); кликнуть Apply на low-risk действии (`get_logs`, `describe_resource`). Проверить `executor_applied` в БД и audit log.
3. **Все preprod-squad-***: постепенный rollout по одному namespace в день, watch на rate `EXECUTOR_APPLY_REFUSED`.
4. **Production-squad-N**: та же процедура, `low`-risk только первую неделю, потом `medium` после одного чистого инцидента.
5. **HIGH-risk auto-apply никогда** — by design. HIGH-risk intent кнопку не получает.

См. [docs/RUNBOOK.md → Executor incidents](docs/RUNBOOK.md#executor-incidents) для операционных процедур.

### Документация

| Документ | EN | RU |
|---|---|---|
| Архитектура | [ARCHITECTURE.md](docs/ARCHITECTURE.md) | [ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md) |
| Боевые прогоны | [RUNBOOK.md](docs/RUNBOOK.md) | [RUNBOOK.ru.md](docs/RUNBOOK.ru.md) |
| Модули | [MODULE_DOCS.md](docs/MODULE_DOCS.md) | [MODULE_DOCS.ru.md](docs/MODULE_DOCS.ru.md) |
| Audit trail (OTEL) | [AUDIT.md](docs/AUDIT.md) | — |
| Semantic Contract | [SEMANTIC_CONTRACT.md](docs/SEMANTIC_CONTRACT.md) | — |
| FAQ | [FAQ.md](docs/FAQ.md) | [FAQ.ru.md](docs/FAQ.ru.md) |
| DR Plan | [DR.md](docs/DR.md) | — |
| Changelog | [CHANGELOG.md](CHANGELOG.md) | — |
