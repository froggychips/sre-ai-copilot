# SRE AI Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-4691%20pass-brightgreen.svg)](tests)
[![Checks](https://img.shields.io/badge/ruff%20%2B%20mypy-clean-brightgreen.svg)](.github/workflows)
[![Release](https://img.shields.io/badge/release-v1.0.33-blue)](CHANGELOG.md)

**Homepage:** [sre.froggychips.xyz](https://sre.froggychips.xyz) · **Stack:** FastAPI + Celery + Postgres-KG + MCP (LLM optional)

> **Kubernetes alerts enriched from a knowledge graph of the cluster, plus remediation that a human approves in Discord.** In production the alert path is deterministic: no LLM call decides what a card says or whether a `kubectl` runs. A remediation is proposed only by a playbook from the registry, checked by a server-side policy gate, and applied only after a click from an operator on the allowlist. An LLM RCA pipeline exists in the code but is switched off in production.

> **[English](#english) · [Русский](#русский)**

![Demo: enriched critical alerts, KG cascade reasoning and daily digest in Discord](docs/assets/copilot-demo.gif)

*Output formats on synthetic data: user-facing endpoint-down critical with a `kubectl` hint, slow-crashloop catcher, deploy-regression enrichment with blast radius, KG upstream-cascade reasoning, resolved notice and the daily cluster digest. Source page: [docs/assets/demo-alerts.html](docs/assets/demo-alerts.html).*

---

<a name="english"></a>
## English

**SRE AI Copilot** is a backend service that receives Alertmanager webhooks, records every alert in a Postgres knowledge graph (KG) of the cluster, and posts deterministically enriched cards to Discord. For a small set of alert types it proposes a remediation from a playbook, with approve / decline buttons.

### What runs in production

- **KG-first enrichment → Discord.** `prod-*`, `preprod-*` and alerts without a namespace (nodes, control plane) go to `/webhooks/alertmanager/enrich-and-forward`: the alert is written to the KG and an enriched card is posted to the incident channel. Enrichment is rules + graph, no LLM: rule facts with evidence (`verdict` found / absent / unknown, `epistemic`, `provenance`; a failed source answers `?`, not «not found»), recent deploys, nearby alerts and open incidents, owner team, blast radius. Meta-aggregate alerts and `KubeDeploymentGenerationMismatch` churn with healthy replicas are rendered muted (grey, 🔇, no mention) instead of dropped.
- **Attribution to the k8s object, not to the exporter** (`app/services/alert_target.py`, 1.0.31): one label-priority chain (workload → CronJob/Job → HPA/PDB → node → PVC → pod → scrape job → service → namespace) used by both the store path and the card; the outcome is stored in `kg_alerts.raw.target_kind`.
- **Less channel noise** (1.0.31): a «✅ resolved» card only for an episode of at least 30 min (`DISCORD_RESOLVED_NOTICE_MIN_MINUTES`); an Alertmanager repeat of a still-firing alert no longer posts «🌀 RESURFACED» (`ALERT_SUPPRESS_AM_REPEATS`).
- **KG as history.** `squad-*`, `preupdate-*` and every `severity=info` alert go to `/webhooks/alertmanager/store`: KG only, nothing in Discord — a decision, not a gap. The graph holds incidents (`kg_incidents`, one open per service), an incident timeline, blast radius with evidence per path, rollouts detected from the cluster (`kg_deploy_watch`), node zones and traffic entrypoints. Queryable via `/kg/*`, MCP tools and the squad dashboard.
- **Deterministic remediation bridge** (`app/remediation/`, 1.0.31; runs in Celery since 1.0.32): rule facts + live pod state → `match_playbooks` → intent built by the server from the playbook's `plan.steps` → policy gate. On `approve` the target is snapshotted, dry-run against the API server, and a card with **Apply / Decline** buttons is posted to the incident channel; on `block` (prod / preprod / system) the regular card only gets a «blocked by policy» line. Playbooks reachable by alert name: `restart_crashloop_deployment` and `scale_out_cpu_throttled_deployment` (the latter does not produce a card yet: the bridge does not compute the target `replicas`, plan rendering fails); `approve` is allowed only for dev / squad namespaces. The same proposal is not repeated for 6 h. For squads this is a narrow exception to «store only»: a card appears only when a playbook applies.
- **Buttons: allowlist only.** A click is accepted only from a user in `DISCORD_APPROVERS_USER_IDS` (fail-closed, roles are not used). In this deployment the Discord application's Interactions Endpoint points to a separate bot service, which relays `approve:` / `decline:` clicks to the copilot's `/discord/interactions`; the copilot verifies the Ed25519 signature itself.
- **Guarded write path** (compressed; details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/REMEDIATION_PLAYBOOK_CONTRACT.md](docs/REMEDIATION_PLAYBOOK_CONTRACT.md)): deterministic policy gate over 8 risk axes, target identity check before the write (`target_reincarnated`), re-dry-run, matching intent signature and a fresh `ActionApproval` (expires after 1 h), `in_flight` claim before `kubectl`, a separate executor with its own ServiceAccount, and outcome verification at +5 / +15 min (`verified` / `failed` / `pending` / `unknown`). Every step is an OTEL / audit event ([docs/AUDIT.md](docs/AUDIT.md)).
- **Remediation events from an external squad medic** are received over HMAC into `kg_remediation_events`; `fixed` means the stand was healthy after treatment.
- **Self-health and reconciliation.** `copilot_self_health_*` metrics with `VMRule sre-ai-copilot` alerts; a weekly reconciliation against the cluster (`weekly_reconciliation`, Mon 06:00 UTC, #stats): alert coverage vs VictoriaMetrics `ALERTS`, table growth, backup age, share of unattributed alerts, confirmed fixes — an unavailable source reads «not measured», not zero.
- **Digests**: daily cluster digest (`daily_stats_digest`), chronic alerts digest every 6 h (`chronic_alerts_digest`, opt-in `CHRONIC_DIGEST_ENABLED`) with a section for alerts firing continuously for more than 24 h.

**Recent changes (1.0.28 → 1.0.33)** — details in [CHANGELOG.md](CHANGELOG.md):

- separate `sync` queue and `copilot-worker-sync` Deployment for heavy KG syncs; `single_instance` on beat tasks (1.0.28);
- statics builds stored as one `kg_deployments` row per namespace instead of a broadcast to every service (1.0.28);
- retention for event tables (`EVENT_RETENTION_TARGETS`) next to the 30-day `kg_service_health` retention; Postgres backup CronJob synced with the cluster (dump on PVC, optional S3 upload) (1.0.28);
- weekly reconciliation (1.0.30); remediation bridge with buttons, resolved ≥ 30 min, no repeat RESURFACED, object attribution (1.0.31);
- bridge moved out of the API event loop into the Celery task `remediation_bridge`, enabled per namespace prefix via `REMEDIATION_BRIDGE_NAMESPACES` (1.0.32);
- PriorityClass `sre-ai-copilot-stateful` for postgres / redis (evicted last); logging configured in every process (`LOG_LEVEL`, httpx / httpcore at WARNING); KG upserts skip unchanged rows; drift mark removed from nodes of a re-created namespace; Python 3.13 (1.0.33).

### LLM pipeline (implemented, off in production)

`POST /webhooks/alertmanager` runs the full LLM pipeline: deterministic diagnostics → multi-hypothesis fan-out → fact critic → synthesis → `FixAgent` with a structured `ExecutionIntent` → optional executor dry-run and Apply button. No Alertmanager route points there: `LLM_PIPELINE_ENABLED=false`, and enabling it requires a finite `LLM_DAILY_BUDGET_USD` (the config validator refuses to start otherwise). Stages, prompts and the golden eval set: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [tests/golden/README.md](tests/golden/README.md).

### Tech stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Queue | Celery + Redis (queues `celery`, `sync`, `executor`) |
| Database | PostgreSQL + SQLAlchemy (SQLite for local dev) |
| LLM | Anthropic Claude (API key or `claude --print` CLI) — LLM pipeline only, off in production |
| Observability | Prometheus / VictoriaMetrics, OpenTelemetry → Tempo, structlog |
| Integrations | Discord, Kubernetes, Jira, TeamCity (MCP), VictoriaMetrics |
| Deploy | TeamCity build + `deploy.sh` with raw manifests in `k8s/` |

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

> `LLM_BACKEND=claude_cli` is a subprocess wrapper around `claude --print`; it matters only for the LLM pipeline.

**Main `.env` keys:**

| Key | Purpose | Required |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Alert cards | prod |
| `DISCORD_PUBLIC_KEY` | Ed25519 key for `/discord/interactions` signature verification | for buttons |
| `DISCORD_APPROVERS_USER_IDS` | Comma-separated Discord user IDs allowed to press Apply / Decline; empty = nobody | for buttons |
| `DISCORD_INCIDENT_CHANNEL_ID` | Channel for remediation proposal cards (posted by the bot) | for the bridge |
| `DISCORD_DRY_RUN` | `true` = log instead of posting to Discord | dev |
| `ALERTMANAGER_WEBHOOK_SECRET` | HMAC-SHA256 webhook auth — mandatory in `ENV=production` | prod |
| `ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS` / `DISCORD_INTERACTION_MAX_AGE_SECONDS` | Anti-replay windows; default 300 | optional |
| `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP` | Reject body-only HMAC without a timestamp; default `false` | optional |
| `JWT_PUBLIC_KEY` | `/copilot` endpoint auth | prod |
| `VICTORIA_METRICS_URL` | Pod metrics, cluster health snapshot, reconciliation | optional |
| `JIRA_*` / `TEAMCITY_MCP_URL` / `TEAMCITY_MCP_TOKEN` | Jira and deploy context | optional |
| `REMEDIATION_BRIDGE_ENABLED` / `REMEDIATION_BRIDGE_NAMESPACES` | Deterministic bridge; namespace prefixes, empty = all; default off | optional |
| `EXECUTOR_ENABLED` / `EXECUTOR_APPROVAL_ENABLED` | Executor dry-run / Apply buttons (both needed for buttons); default `false` | optional |
| `LLM_PIPELINE_ENABLED` / `LLM_DAILY_BUDGET_USD` / `ANTHROPIC_API_KEY` | LLM pipeline; default off | optional |
| `ROLLOUT_SUPPRESS_ENABLED` / `META_NOISE_ENABLED` / `GEN_MISMATCH_NOISE_ENABLED` | Mute deploy-window / meta-aggregate / generation-mismatch noise; default `true` | optional |
| `DISCORD_RESOLVED_NOTICE_MIN_MINUTES` | Minimum episode length for a resolved card; default 30, `0` = all | optional |
| `WEEKLY_RECONCILIATION_ENABLED` / `CHRONIC_DIGEST_ENABLED` | Weekly reconciliation (default on) / chronic digest (default off) | optional |
| `LOG_LEVEL` | Log level of every process; default `INFO` | optional |
| `PIPELINE_DIRECT_INVOKE` | Run pipeline inline (skip Celery) — for local e2e | dev |

### Discord integration

Cards go to `DISCORD_WEBHOOK_URL`; remediation proposals are posted by the bot to `DISCORD_INCIDENT_CHANNEL_ID`. Buttons need an Interactions Endpoint URL in the Discord Developer Portal (`https://<your-host>/discord/interactions`, or a relay that forwards to it) and `DISCORD_PUBLIC_KEY`. For local testing expose the service with [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/): `cloudflared tunnel --url http://localhost:8000`. To take over a channel, remove the direct Alertmanager → Discord webhook for those alerts and set `DISCORD_DRY_RUN=false`.

### Deploy

Components in namespace `sre-ai`: Deployments `sre-ai-api` (2, FastAPI :8000), `copilot-worker` (2, queue `celery`), `copilot-worker-sync` (1, queue `sync`), `copilot-executor` (1, queue `executor`, separate ServiceAccount with write rights), `copilot-beat` (1); StatefulSets `postgres` and `redis` (local-path volumes, PriorityClass `sre-ai-copilot-stateful`); CronJobs `postgres-backup`, `squad-dashboard`, `ci-deadman`. The Helm chart was removed in #524; manifests live only in [`k8s/`](k8s/).

1. **Release:** bump `app/__init__.py`, the README badge and CHANGELOG → PR → merge → tag `vX.Y.Z` → GitHub Release.
2. **Build and roll out:** TeamCity `SreAiCopilotBuildAndDeploy` with parameter `TAG=X.Y.Z` builds `docker.lastoasisgame.com/wo/sre-ai-copilot:X.Y.Z` and runs `kubectl set image` on the five Deployments.
3. **Version in cards:** `kubectl -n sre-ai set env deploy/<each of the five> BUILD_VERSION=X.Y.Z` — the card footer reads the env, not the image tag.
4. **Manifests:** `IMAGE_TAG=X.Y.Z ./deploy.sh sre-ai` only when Deployment / NetworkPolicy / RBAC / PriorityClass manifests change (it runs migrations as a Job first and checks that Secret `sre-ai-secrets` exists; it never applies the secrets template). `deploy.sh` does not apply the postgres / redis StatefulSets: change them with `kubectl patch`, not `apply` — the live StatefulSets carry node pinning that is not in git.
5. **Manual build** (without TeamCity): `docker buildx build --platform linux/amd64 --provenance=false --sbom=false ... --push`.

**Rollback:** `kubectl set image` to the previous tag + `BUILD_VERSION`. Do not re-run TeamCity with an old `TAG`: it builds `master`, so it would overwrite the old image with current code. Enable new flags on one namespace under load first, then everywhere. Procedure and migrations: [docs/RUNBOOK.md](docs/RUNBOOK.md#production-rollout-the-actual-procedure-2026-08-08); backup and restore: [docs/DR.md](docs/DR.md).

**Backup:** `postgres-backup` (02:00 UTC) writes `pg_dump` to PVC `postgres-backup-data` (keeps the last N), then uploads it with `aws s3 cp` when Secret `postgres-backup-s3` exists.

### Alert path (what actually runs)

Routing lives in [`k8s/vmalertmanagerconfig.yaml`](k8s/vmalertmanagerconfig.yaml); the api exposes three Alertmanager endpoints (`app/api/webhooks.py`):

| Endpoint | What it does | Who is routed there |
|---|---|---|
| `POST /webhooks/alertmanager/enrich-and-forward` | Writes the alert into the KG **and** posts an enriched card to Discord — deterministic enrichment, no LLM | `prod-*`, `preprod-*`, and everything without a namespace (node and control-plane alerts) |
| `POST /webhooks/alertmanager/store` | KG only: history, incidents, evidence for playbooks. **Nothing goes to Discord** (except a remediation proposal) | `squad-*`, `preupdate-*`, and every `severity=info` alert |
| `POST /webhooks/alertmanager` | The full LLM pipeline | Nobody: `LLM_PIPELINE_ENABLED=false` |

`Watchdog` and `severity=none` go to a blackhole receiver. On both live paths the API only does a cheap `wants_bridge` check and a Redis `SET NX` enqueue dedup, then hands the alert to the `remediation_bridge` Celery task; Redis down → no task (fail-closed).

### API endpoints

| Endpoint | Description |
|---|---|
| `POST /webhooks/alertmanager` | Alertmanager batch → full LLM pipeline; off in production |
| `POST /webhooks/alertmanager/store` | Same payload → KG only |
| `POST /webhooks/alertmanager/enrich-and-forward` | Same payload → KG + enriched Discord card |
| `GET /webhooks/status/{task_id}` | Celery task status |
| `POST /discord/interactions` | Discord button interactions (Ed25519-verified) |
| `POST /copilot` | Conversational analysis |
| `GET /jobs/{task_id}` | Copilot task status |
| `POST /approvals/{id}/approve\|reject` | Human approval |
| `POST /replay/{incident_id}` | Re-run historical incident |
| `POST /evaluation/{id}/submit` | Feedback submission |
| `GET /kg/incidents` | Incidents from the graph (`status`, `namespace`, `service`, `include_noise`) |
| `GET /kg/incidents/{id}` | Incident with its alerts |
| `GET /kg/incidents/{id}/timeline` | Timeline across six tables + operational memory + `unknowns` |
| `GET /kg/blast-radius?namespace=&service=` | Who breaks if X breaks, with epistemic evidence per path |
| `GET /healthz`, `GET /readyz` | Liveness / readiness |

### Combat runs (accuracy history)

*Historical: hand-checked runs of the LLM pipeline, spring 2026.* Full details: [docs/RUNBOOK.md](docs/RUNBOOK.md)

| Run | Incident | Result | Problem found | Fix shipped |
|---|---|---|---|---|
| 1 | Smoke SIGSEGV | ❌ unresolved | `OOMKilledRule` text-regex false positive on other pods' events | Structured gate: target exit ≠ 137 → `observed=False` |
| 2 | Smoke SIGSEGV | ❌ unresolved | `oom_killed` + `process_crash` both True → FactCritic kills all hypotheses | `MUTUALLY_EXCLUSIVE_PAIRS` conflict detection, confidence cap 0.60 |
| 3 | Live notificator exit 139 | ❌ unresolved | Same OOM false positive on real cluster; KG polluted with "Manual triage required" | OOM structured gate deployed; KG quality gate; `_is_quality_cause()` filter |
| 4 | Live notificator exit 139 | ✅ **resolved** | Jira `GET /search` → 410 Gone (graceful degrade) | All fixes active; cause: "Nil pointer dereference in startup initialization path" |
| 5 | Live preprod pod crash | ✅ **resolved** | TC context missing (`no_deploys_or_no_timestamp`) — flagged as gap, not false root cause | Correct cautious behaviour; TC MCP URL not configured locally |
| 6 | Live preprod pod crash | ✅ **resolved** | Pipeline self-diagnosed a false refutation in synthesis | Correct — synthesis explicitly noted the contradiction and recommended manual check |

Being hand-checked is exactly the problem these runs illustrate. The **[golden set](tests/golden/README.md)** closes it: 20 frozen incidents with expectations, run on every PR in `replay` mode (recorded LLM answers — no network, no key, deterministic) and against the real model on a schedule (`.github/workflows/eval-live.yml`). Runs 1–3 are now cases `001` and `013`.

```bash
python scripts/eval_golden.py --mode replay --check-baseline
```

### Security

- **No model decides a write.** Intents are structured (`ExecutionIntent`, pydantic, `FORBIDDEN_NAMESPACES` rejected at parse time); `DSLTranslator` builds the `kubectl` command deterministically; `K8sSecurityGuard` validates `(verb, resource, namespace)` structurally; the server-side policy gate (`app/remediation`, 8 risk axes) recomputes risk and blocks prod / system / data-plane / irreversible actions — an LLM `risk` field is advisory only. `apply_intent` requires a matching intent signature (TOCTOU) and a recorded `ActionApproval`, with a row lock against double apply.
- Tiered namespace policy in `K8sSecurityGuard.validate`: `prod`/`preprod` read-only; `squad-*` write via approval; `kube-*`/`mcp` forbidden.
- `SAFE_MODE=true` enforced in `ENV=production` (config validator raises otherwise).
- Buttons: approver allowlist `DISCORD_APPROVERS_USER_IDS`, fail-closed; Ed25519 signature and timestamp freshness on every interaction (anti-replay).
- Alertmanager webhook: HMAC-SHA256 on the body, mandatory in production; with `X-Alertmanager-Timestamp` the HMAC covers `ts.body` and a freshness window applies.
- Prompt injection guard (LLM pipeline): injection patterns blocked, oversized input truncated rather than rejected.
- Full OTEL audit trail and `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED` events — see [docs/AUDIT.md](docs/AUDIT.md).

### Roadmap — Execution

| # | Step | Status |
|---|---|---|
| 1 | `FixAgent` emits structured `ExecutionIntent` alongside prose | ✅ v0.7.0 (PR #23) |
| 2 | `executor` stage with `dry_run=True` + `K8sSecurityGuard.validate` | ✅ v0.7.0 (PR #26) |
| 3 | Discord Apply consumer with two-click confirm → real `kubectl` under guard | ✅ v0.7.0 (PR #27) |
| 4 | Remediation verification — target identity snapshot, delayed outcome check | ✅ v1.0.7 (PR #369) |
| 5 | Deterministic bridge rule → playbook → Apply / Decline buttons, enabled in production (`REMEDIATION_BRIDGE_ENABLED`, `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED` = true, approver allowlist) | ✅ v1.0.31 |
| 6 | Bridge in Celery instead of the API event loop; per-namespace enablement | ✅ v1.0.32 |

Rules that stay: `approve` only for dev / squad namespaces, prod / preprod / system are blocked by policy; no HIGH-risk auto-apply; new flags go to one namespace under load before all. Operations: [docs/RUNBOOK.md → Executor incidents](docs/RUNBOOK.md#executor-incidents).

### Roadmap — Knowledge graph

| # | Step | Status |
|---|---|---|
| 1 | Evidence semantics: `verdict` / `epistemic` / `provenance`, Known Unknowns via `source_status` | ✅ v1.0.7 (PR #365) |
| 2 | Incident as a graph object (`kg_incidents`) + timeline | ✅ v1.0.7 (PR #366) |
| 3 | Blast radius with evidence per path | ✅ v1.0.7 (PR #368) |
| 4 | Operational memory: evidence → diagnosis → decision → action → verification | ✅ v1.0.8 (PR #371) |
| 5 | One point-in-time incident context from the graph for rules, model and card (`app/context/kg_incident_context.py`) | ✅ v1.0.20 |
| 6 | Node zones and traffic entrypoints (`kg_nodes`, `kg_entrypoints`), databases as graph nodes | ✅ v1.0.24 |
| 7 | Alert attributed to its k8s object instead of the metrics exporter | ✅ v1.0.25, v1.0.31 |
| 8 | Incident correlation across services — as edges between incidents | ⬜ not implemented |
| 9 | Deterministic, evidence-backed RCA | ⬜ after 8 |

### Documentation

| Document | EN | RU |
|---|---|---|
| Architecture (incl. LLM pipeline) | [ARCHITECTURE.md](docs/ARCHITECTURE.md) | [ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md) |
| Runbook / rollout / combat runs | [RUNBOOK.md](docs/RUNBOOK.md) | [RUNBOOK.ru.md](docs/RUNBOOK.ru.md) |
| Module docs | [MODULE_DOCS.md](docs/MODULE_DOCS.md) | [MODULE_DOCS.ru.md](docs/MODULE_DOCS.ru.md) |
| FAQ | [FAQ.md](docs/FAQ.md) | [FAQ.ru.md](docs/FAQ.ru.md) |
| Metrics (signals, self-health) | [METRICS.md](docs/METRICS.md) | [METRICS.ru.md](docs/METRICS.ru.md) |
| Remediation playbook contract | — | [REMEDIATION_PLAYBOOK_CONTRACT.md](docs/REMEDIATION_PLAYBOOK_CONTRACT.md) |
| KG schema / quality contract | — | [KG_SCHEMA_CONTRACT.md](docs/KG_SCHEMA_CONTRACT.md) |
| Semantic contract | [SEMANTIC_CONTRACT.md](docs/SEMANTIC_CONTRACT.md) | — |
| Audit trail (OTEL) | [AUDIT.md](docs/AUDIT.md) | — |
| DR plan | [DR.md](docs/DR.md) | — |
| Post-mortem 2026-08-08 (historical) | [POSTMORTEM_2026_08_08.md](docs/POSTMORTEM_2026_08_08.md) | [POSTMORTEM_2026_08_08.ru.md](docs/POSTMORTEM_2026_08_08.ru.md) |
| KG quality baseline 2026-05-24 (historical) | — | [quality_report_baseline_2026_05_24.md](docs/quality_report_baseline_2026_05_24.md) |
| Golden eval set | [tests/golden/README.md](tests/golden/README.md) | — |
| Changelog | — | [CHANGELOG.md](CHANGELOG.md) |

### sre-ai-copilot vs froggy-sre

| | sre-ai-copilot | [froggy-sre](https://github.com/froggychips/froggy-sre) |
|---|---|---|
| **Trigger** | Alertmanager webhook (headless) | MCP tool call from Claude Code |
| **Runtime** | Kubernetes pods | macOS dev machine |
| **Analysis** | Deterministic rules + KG (LLM pipeline off) | Froggy local → Anthropic fallback |
| **k8s context** | In-cluster Kubernetes SDK | `kubectl` via kubeconfig |
| **Storage** | PostgreSQL + Celery queue | `~/.froggy-sre/incidents/` (local JSON) |
| **Notifications** | Discord | Reply in Claude Code |
| **When to use** | Persistent headless alerting in production | Interactive incident analysis via Claude Code |

---

<a name="русский"></a>
## Русский

**SRE AI Copilot** — backend-сервис: принимает вебхуки Alertmanager, записывает каждый алерт в граф знаний кластера (KG) на Postgres и постит в Discord карточки с детерминированным обогащением. Для небольшого набора типов алертов предлагает действие из playbook-а с кнопками «Применить / Отклонить».

### Что работает в проде

- **Обогащение из KG → Discord.** `prod-*`, `preprod-*` и алерты без namespace (ноды, control plane) идут в `/webhooks/alertmanager/enrich-and-forward`: алерт пишется в граф, в канал инцидентов уходит обогащённая карточка. Обогащение — правила и граф, без LLM: факты правил со свидетельствами (`verdict` found / absent / unknown, `epistemic`, `provenance`; упавший источник отвечает `?`, а не «не нашли»), недавние деплои, соседние алерты и открытые инциденты, команда-владелец, blast radius. Мета-агрегаты и churn `KubeDeploymentGenerationMismatch` при здоровых репликах рисуются приглушённо (серым, 🔇, без упоминания), а не выбрасываются.
- **Атрибуция к k8s-объекту, а не к экспортеру** (`app/services/alert_target.py`, 1.0.31): одна цепочка приоритета меток (workload → CronJob/Job → HPA/PDB → нода → PVC → под → scrape job → service → namespace) для store-пути и для карточки; исход — в `kg_alerts.raw.target_kind`.
- **Меньше шума в канале** (1.0.31): карточка «✅ resolved» только за эпизод от 30 минут (`DISCORD_RESOLVED_NOTICE_MIN_MINUTES`); повтор горящего алерта от Alertmanager больше не постит «🌀 RESURFACED» (`ALERT_SUPPRESS_AM_REPEATS`).
- **KG как история.** `squad-*`, `preupdate-*` и все алерты `severity=info` идут в `/webhooks/alertmanager/store`: только граф, в Discord ничего — это решение, а не дыра. В графе — инциденты (`kg_incidents`, один открытый на сервис), timeline инцидента, blast radius со свидетельствами на каждом пути, выкаты, замеченные в самом кластере (`kg_deploy_watch`), зоны нод и точки входа трафика. Доступно через `/kg/*`, MCP-инструменты и витрину сквадов.
- **Детерминированный мост remediation** (`app/remediation/`, 1.0.31; в Celery с 1.0.32): факты правил + живое состояние пода → `match_playbooks` → intent из `plan.steps` playbook-а (собирает сервер) → policy-gate. При `approve` — снимок цели, server dry-run и карточка с кнопками **Применить / Отклонить** в канале инцидентов; при `block` (prod / preprod / system) в обычной карточке только строка «заблокирован policy». Playbook-и, доступные мосту по имени алерта: `restart_crashloop_deployment` и `scale_out_cpu_throttled_deployment` (второй карточку пока не даёт: мост не вычисляет целевое `replicas`, рендер плана падает); `approve` разрешён только для dev / squad. Одно и то же предложение не повторяется 6 ч. Для сквадов это узкое исключение из «только в KG»: карточка появляется, только когда playbook применим.
- **Кнопки — только allowlist.** Клик принимается только от пользователя из `DISCORD_APPROVERS_USER_IDS` (fail-closed, роли не используются). В этой установке Interactions Endpoint Discord-приложения указывает на отдельный сервис бота, который пересылает клики `approve:` / `decline:` в `/discord/interactions` копилота; подпись Ed25519 копилот проверяет сам.
- **Защищённый путь записи** (кратко; подробно — [docs/ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md) и [docs/REMEDIATION_PLAYBOOK_CONTRACT.md](docs/REMEDIATION_PLAYBOOK_CONTRACT.md)): детерминированный policy-gate по 8 осям риска, проверка идентичности цели перед записью (`target_reincarnated`), повторный dry-run, совпадение подписи intent-а и свежий `ActionApproval` (протухает через 1 ч), claim `in_flight` до `kubectl`, отдельный executor со своим ServiceAccount и проверка исхода через 5 и 15 минут (`verified` / `failed` / `pending` / `unknown`). Каждый шаг — событие OTEL / аудита ([docs/AUDIT.md](docs/AUDIT.md)).
- **События лечения от внешнего медика стендов** принимаются по HMAC в `kg_remediation_events`; `fixed` — стенд здоров после лечения.
- **Self-health и сверка.** Метрики `copilot_self_health_*` с алертами `VMRule sre-ai-copilot`; еженедельная сверка с кластером (`weekly_reconciliation`, пн 06:00 UTC, #stats): покрытие алертов против `ALERTS` в VictoriaMetrics, рост таблиц, возраст бэкапа, доля алертов без атрибуции, подтверждённые починки — недоступный источник даёт «не измерено», а не ноль.
- **Дайджесты**: ежедневный по кластеру (`daily_stats_digest`), хронические алерты раз в 6 ч (`chronic_alerts_digest`, opt-in `CHRONIC_DIGEST_ENABLED`) с секцией алертов, горящих непрерывно дольше 24 ч.

**Что нового (1.0.28 → 1.0.33)** — подробно в [CHANGELOG.md](CHANGELOG.md):

- отдельная очередь `sync` и Deployment `copilot-worker-sync` для тяжёлых синков KG; `single_instance` на beat-задачах (1.0.28);
- сборки статики — одна строка `kg_deployments` на namespace вместо рассылки на все сервисы (1.0.28);
- ретеншен таблиц событий (`EVENT_RETENTION_TARGETS`) рядом с 30-дневным ретеншеном `kg_service_health`; CronJob бэкапа Postgres сверен с кластером (дамп на PVC, опциональная выгрузка в S3) (1.0.28);
- еженедельная сверка (1.0.30); мост remediation с кнопками, resolved ≥ 30 мин, без повторного RESURFACED, атрибуция к объекту (1.0.31);
- мост вынесен из event loop API в Celery-задачу `remediation_bridge`, включение по префиксам namespace через `REMEDIATION_BRIDGE_NAMESPACES` (1.0.32);
- PriorityClass `sre-ai-copilot-stateful` у postgres / redis (выселяются последними); логирование настроено в каждом процессе (`LOG_LEVEL`, httpx / httpcore на WARNING); upsert-ы KG не переписывают неизменившиеся строки; пометка дрейфа снимается с узлов пересозданного namespace; Python 3.13 (1.0.33).

### LLM-конвейер (реализован, в проде выключен)

`POST /webhooks/alertmanager` запускает полный LLM-конвейер: детерминированная диагностика → многогипотезный fan-out → критик фактов → синтез → `FixAgent` со структурным `ExecutionIntent` → опционально dry-run executor-а и кнопка Apply. Ни один маршрут Alertmanager туда не ведёт: `LLM_PIPELINE_ENABLED=false`, а для включения нужен конечный `LLM_DAILY_BUDGET_USD` (иначе config-validator не даёт стартовать). Стадии, промпты и golden-набор: [docs/ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md), [tests/golden/README.md](tests/golden/README.md).

### Стек

| Слой | Технология |
|---|---|
| API | FastAPI + Uvicorn |
| Очереди | Celery + Redis (очереди `celery`, `sync`, `executor`) |
| База | PostgreSQL + SQLAlchemy (SQLite для локальной разработки) |
| LLM | Anthropic Claude (API key или CLI `claude --print`) — только LLM-конвейер, в проде выключен |
| Наблюдаемость | Prometheus / VictoriaMetrics, OpenTelemetry → Tempo, structlog |
| Интеграции | Discord, Kubernetes, Jira, TeamCity (MCP), VictoriaMetrics |
| Деплой | Сборка TeamCity + `deploy.sh` с сырыми манифестами из `k8s/` |

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

> `LLM_BACKEND=claude_cli` — subprocess-обёртка вокруг `claude --print`; нужна только LLM-конвейеру.

**Основные переменные окружения:**

| Ключ | Назначение | Обязательность |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Карточки алертов | prod |
| `DISCORD_PUBLIC_KEY` | Ed25519-ключ для верификации `/discord/interactions` | для кнопок |
| `DISCORD_APPROVERS_USER_IDS` | Discord ID пользователей через запятую, кому можно жать Применить / Отклонить; пусто — никому | для кнопок |
| `DISCORD_INCIDENT_CHANNEL_ID` | Канал для карточек-предложений моста (постит бот) | для моста |
| `DISCORD_DRY_RUN` | `true` = логировать вместо отправки | dev |
| `ALERTMANAGER_WEBHOOK_SECRET` | HMAC-SHA256 аутентификация вебхука — обязателен в `ENV=production` | prod |
| `ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS` / `DISCORD_INTERACTION_MAX_AGE_SECONDS` | Окна anti-replay; дефолт 300 | опционально |
| `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP` | Отвергать body-only HMAC без timestamp; дефолт `false` | опционально |
| `JWT_PUBLIC_KEY` | Аутентификация `/copilot` | prod |
| `VICTORIA_METRICS_URL` | Метрики пода, снимок кластера, сверка | опционально |
| `JIRA_*` / `TEAMCITY_MCP_URL` / `TEAMCITY_MCP_TOKEN` | Контекст Jira и деплоев | опционально |
| `REMEDIATION_BRIDGE_ENABLED` / `REMEDIATION_BRIDGE_NAMESPACES` | Детерминированный мост; префиксы namespace, пусто — все; по умолчанию выключен | опционально |
| `EXECUTOR_ENABLED` / `EXECUTOR_APPROVAL_ENABLED` | Dry-run executor-а / кнопки Apply (для кнопок нужны оба); по умолчанию `false` | опционально |
| `LLM_PIPELINE_ENABLED` / `LLM_DAILY_BUDGET_USD` / `ANTHROPIC_API_KEY` | LLM-конвейер; по умолчанию выключен | опционально |
| `ROLLOUT_SUPPRESS_ENABLED` / `META_NOISE_ENABLED` / `GEN_MISMATCH_NOISE_ENABLED` | Приглушать шум окна деплоя / мета-агрегатов / generation mismatch; по умолчанию `true` | опционально |
| `DISCORD_RESOLVED_NOTICE_MIN_MINUTES` | Минимальная длина эпизода для resolved-карточки; дефолт 30, `0` — все | опционально |
| `WEEKLY_RECONCILIATION_ENABLED` / `CHRONIC_DIGEST_ENABLED` | Еженедельная сверка (по умолчанию вкл) / хронический дайджест (по умолчанию выкл) | опционально |
| `LOG_LEVEL` | Уровень логов каждого процесса; дефолт `INFO` | опционально |
| `PIPELINE_DIRECT_INVOKE` | Запуск пайплайна inline без Celery — для локального e2e | dev |

### Discord-интеграция

Карточки уходят в `DISCORD_WEBHOOK_URL`; предложения моста бот постит в `DISCORD_INCIDENT_CHANNEL_ID`. Для кнопок нужен Interactions Endpoint URL в Discord Developer Portal (`https://<your-host>/discord/interactions` или relay, который туда пересылает) и `DISCORD_PUBLIC_KEY`. Для локального теста — [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/): `cloudflared tunnel --url http://localhost:8000`. Чтобы забрать канал, уберите прямой вебхук Alertmanager → Discord для этих алертов и выставьте `DISCORD_DRY_RUN=false`.

### Деплой

Компоненты в namespace `sre-ai`: Deployment-ы `sre-ai-api` (2, FastAPI :8000), `copilot-worker` (2, очередь `celery`), `copilot-worker-sync` (1, очередь `sync`), `copilot-executor` (1, очередь `executor`, отдельный ServiceAccount с правом записи), `copilot-beat` (1); StatefulSet-ы `postgres` и `redis` (тома local-path, PriorityClass `sre-ai-copilot-stateful`); CronJob-ы `postgres-backup`, `squad-dashboard`, `ci-deadman`. Helm-чарт удалён в #524; манифесты живут только в [`k8s/`](k8s/).

1. **Релиз:** бамп `app/__init__.py`, бейджа README и CHANGELOG → PR → мерж → тег `vX.Y.Z` → GitHub Release.
2. **Сборка и выкат:** TeamCity `SreAiCopilotBuildAndDeploy` с параметром `TAG=X.Y.Z` собирает `docker.lastoasisgame.com/wo/sre-ai-copilot:X.Y.Z` и делает `kubectl set image` на пять Deployment-ов.
3. **Версия в карточках:** `kubectl -n sre-ai set env deploy/<каждый из пяти> BUILD_VERSION=X.Y.Z` — футер карточки читает env, а не тег образа.
4. **Манифесты:** `IMAGE_TAG=X.Y.Z ./deploy.sh sre-ai` — только когда меняются манифесты Deployment / NetworkPolicy / RBAC / PriorityClass (сначала миграции Job-ом, проверяет наличие Secret `sre-ai-secrets`, шаблон секретов не применяет никогда). StatefulSet-ы postgres / redis `deploy.sh` не применяет: менять их `kubectl patch`, не `apply` — у живых StatefulSet-ов есть привязка к ноде, которой нет в git.
5. **Ручная сборка** (без TeamCity): `docker buildx build --platform linux/amd64 --provenance=false --sbom=false ... --push`.

**Откат:** `kubectl set image` на прежний тег + `BUILD_VERSION`. TeamCity со старым `TAG` не запускать: он собирает `master` и перезапишет старый образ текущим кодом. Новые флаги — сначала на одном namespace под нагрузкой, потом на всех. Процедура и миграции: [docs/RUNBOOK.ru.md](docs/RUNBOOK.ru.md#выкат-в-прод-реальная-процедура-08082026); бэкап и восстановление: [docs/DR.md](docs/DR.md).

**Бэкап:** `postgres-backup` (02:00 UTC) пишет `pg_dump` на PVC `postgres-backup-data` (хранит последние N), затем выгружает его `aws s3 cp`, если есть Secret `postgres-backup-s3`.

### Боевой путь алертов

Маршрутизация — в [`k8s/vmalertmanagerconfig.yaml`](k8s/vmalertmanagerconfig.yaml); api отдаёт три endpoint-а для Alertmanager (`app/api/webhooks.py`):

| Endpoint | Что делает | Кто туда маршрутизируется |
|---|---|---|
| `POST /webhooks/alertmanager/enrich-and-forward` | Пишет алерт в KG **и** постит обогащённую карточку в Discord — детерминированно, без LLM | `prod-*`, `preprod-*` и всё без namespace (нодовые и control-plane алерты) |
| `POST /webhooks/alertmanager/store` | Только KG: история, инциденты, свидетельства для playbook-ов. **В Discord ничего** (кроме предложения моста) | `squad-*`, `preupdate-*` и все алерты с `severity=info` |
| `POST /webhooks/alertmanager` | Полный LLM-конвейер | Никто: `LLM_PIPELINE_ENABLED=false` |

`Watchdog` и `severity=none` уходят в blackhole-receiver. На обоих живых путях API делает только дешёвую проверку `wants_bridge` и дедуп постановки через Redis `SET NX`, дальше алерт обрабатывает Celery-задача `remediation_bridge`; Redis недоступен — задача не ставится (fail-closed).

### API endpoints

| Endpoint | Описание |
|---|---|
| `POST /webhooks/alertmanager` | Батч Alertmanager → полный LLM-конвейер; в проде выключен |
| `POST /webhooks/alertmanager/store` | Тот же payload → только KG |
| `POST /webhooks/alertmanager/enrich-and-forward` | Тот же payload → KG + обогащённая карточка в Discord |
| `GET /webhooks/status/{task_id}` | Статус Celery-задачи |
| `POST /discord/interactions` | Discord-взаимодействия с кнопками (Ed25519-верификация) |
| `POST /copilot` | Разговорный анализ |
| `GET /jobs/{task_id}` | Статус copilot-задачи |
| `POST /approvals/{id}/approve\|reject` | Human approval |
| `POST /replay/{incident_id}` | Перезапуск исторического инцидента |
| `POST /evaluation/{id}/submit` | Ручная отправка фидбека |
| `GET /kg/incidents` | Инциденты графа (`status`, `namespace`, `service`, `include_noise`) |
| `GET /kg/incidents/{id}` | Инцидент с его алертами |
| `GET /kg/incidents/{id}/timeline` | Timeline по шести таблицам + операционная память + `unknowns` |
| `GET /kg/blast-radius?namespace=&service=` | Что сломается, если X сломается, с эпистемикой на каждом пути |
| `GET /healthz`, `GET /readyz` | Liveness / readiness |

### Боевые прогоны (история точности)

*Исторический раздел: ручные прогоны LLM-конвейера весной 2026.* Подробно: [docs/RUNBOOK.ru.md](docs/RUNBOOK.ru.md)

| Прогон | Инцидент | Результат | Найденная проблема | Задеплоенный фикс |
|---|---|---|---|---|
| 1 | Smoke SIGSEGV | ❌ unresolved | `OOMKilledRule` text-regex срабатывал на события других подов | Структурный шлюз: exit ≠ 137 → `observed=False` |
| 2 | Smoke SIGSEGV | ❌ unresolved | `oom_killed` + `process_crash` оба True → FactCritic убивает все гипотезы | `MUTUALLY_EXCLUSIVE_PAIRS`, cap конфиденса до 0.60 |
| 3 | Live exit 139 | ❌ unresolved | Тот же false positive на реальном кластере; KG загрязнён | Структурный шлюз OOM + KG quality gate |
| 4 | Live exit 139 | ✅ **resolved** | Jira 410 Gone (graceful degrade) | Все фиксы активны; причина: "Nil pointer dereference…" |
| 5 | Live preprod pod crash | ✅ **resolved** | TC-контекст missing (`no_deploys_or_no_timestamp`) — отмечен как gap, не ложный root cause | Корректная осторожность; `TEAMCITY_MCP_URL` не настроен локально |
| 6 | Live preprod pod crash | ✅ **resolved** | Пайплайн самодиагностировал ложное опровержение в синтезе | Корректно — синтез явно отметил противоречие и рекомендовал ручную проверку |

Проверка руками — ровно та проблема, которую эти прогоны иллюстрируют. Её закрывает **[golden-набор](tests/golden/README.md)**: 20 зафиксированных инцидентов с ожиданиями, прогон на каждом PR в режиме `replay` (записанные ответы LLM — без сети, без ключа, детерминированно) и против живой модели по расписанию (`.github/workflows/eval-live.yml`). Прогоны 1–3 стали кейсами `001` и `013`.

```bash
python scripts/eval_golden.py --mode replay --check-baseline
```

### Безопасность

- **Запись не решает модель.** Intent-ы структурные (`ExecutionIntent`, pydantic, `FORBIDDEN_NAMESPACES` отбрасываются на парсе); `DSLTranslator` детерминированно строит команду `kubectl`; `K8sSecurityGuard` структурно валидирует `(verb, resource, namespace)`; серверный policy-gate (`app/remediation`, 8 осей риска) пересчитывает риск и блокирует prod / system / data-plane / необратимое — поле `risk` от LLM лишь advisory. `apply_intent` требует совпадения подписи intent-а (TOCTOU) и записи `ActionApproval`, с row-lock против двойного apply.
- Tiered namespace policy в `K8sSecurityGuard.validate`: `prod`/`preprod` — read-only; `squad-*` — write через approval; `kube-*`/`mcp` — forbidden.
- `SAFE_MODE=true` принудительно в `ENV=production` (иначе config-validator валит старт).
- Кнопки: allowlist `DISCORD_APPROVERS_USER_IDS`, fail-closed; подпись Ed25519 и свежесть timestamp на каждом взаимодействии (anti-replay).
- Вебхук Alertmanager: HMAC-SHA256 на body, обязателен в production; при `X-Alertmanager-Timestamp` HMAC считается над `ts.body` и действует окно свежести.
- Защита от prompt injection (LLM-конвейер): injection-паттерны блокируются, слишком длинный ввод обрезается, а не отклоняется.
- Полный OTEL audit trail + события `EXECUTOR_APPLIED` / `EXECUTOR_APPLY_REFUSED` — см. [docs/AUDIT.md](docs/AUDIT.md).

### Roadmap — Execution

| # | Шаг | Статус |
|---|---|---|
| 1 | `FixAgent` отдаёт структурный `ExecutionIntent` рядом с prose | ✅ v0.7.0 (PR #23) |
| 2 | `executor`-стадия с `dry_run=True` + `K8sSecurityGuard.validate` | ✅ v0.7.0 (PR #26) |
| 3 | Discord Apply consumer с двухшаговым confirm → реальный `kubectl` под guard | ✅ v0.7.0 (PR #27) |
| 4 | Верификация remediation — снимок идентичности цели, отложенная проверка исхода | ✅ v1.0.7 (PR #369) |
| 5 | Детерминированный мост правило → playbook → кнопки Применить / Отклонить, включён в проде (`REMEDIATION_BRIDGE_ENABLED`, `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED` = true, allowlist approver-ов) | ✅ v1.0.31 |
| 6 | Мост в Celery вместо event loop API; включение по namespace | ✅ v1.0.32 |

Правила остаются: `approve` только для dev / squad, prod / preprod / system блокирует policy; HIGH-risk auto-apply не бывает; новые флаги — сначала на один namespace под нагрузкой, потом на все. Эксплуатация: [docs/RUNBOOK.ru.md → Executor-инциденты](docs/RUNBOOK.ru.md#executor-инциденты).

### Roadmap — Knowledge graph

| # | Шаг | Статус |
|---|---|---|
| 1 | Семантика свидетельств: `verdict` / `epistemic` / `provenance`, Known Unknowns через `source_status` | ✅ v1.0.7 (PR #365) |
| 2 | Incident как объект графа (`kg_incidents`) + timeline | ✅ v1.0.7 (PR #366) |
| 3 | Blast radius с доказательствами на каждом пути | ✅ v1.0.7 (PR #368) |
| 4 | Операционная память: свидетельства → диагноз → решение → действие → верификация | ✅ v1.0.8 (PR #371) |
| 5 | Единый контекст инцидента из графа на момент T — для правил, модели и карточки (`app/context/kg_incident_context.py`) | ✅ v1.0.20 |
| 6 | Зоны нод и точки входа трафика (`kg_nodes`, `kg_entrypoints`), базы как узлы графа | ✅ v1.0.24 |
| 7 | Алерт привязан к своему k8s-объекту, а не к экспортеру метрик | ✅ v1.0.25, v1.0.31 |
| 8 | Корреляция инцидентов между сервисами — рёбрами между инцидентами | ⬜ не реализовано |
| 9 | Детерминированный RCA на доказательствах | ⬜ после п.8 |

### Документация

| Документ | EN | RU |
|---|---|---|
| Архитектура (в т.ч. LLM-конвейер) | [ARCHITECTURE.md](docs/ARCHITECTURE.md) | [ARCHITECTURE.ru.md](docs/ARCHITECTURE.ru.md) |
| Runbook / выкат / боевые прогоны | [RUNBOOK.md](docs/RUNBOOK.md) | [RUNBOOK.ru.md](docs/RUNBOOK.ru.md) |
| Модули | [MODULE_DOCS.md](docs/MODULE_DOCS.md) | [MODULE_DOCS.ru.md](docs/MODULE_DOCS.ru.md) |
| FAQ | [FAQ.md](docs/FAQ.md) | [FAQ.ru.md](docs/FAQ.ru.md) |
| Метрики (сигналы, self-health) | [METRICS.md](docs/METRICS.md) | [METRICS.ru.md](docs/METRICS.ru.md) |
| Контракт playbook-а remediation | — | [REMEDIATION_PLAYBOOK_CONTRACT.md](docs/REMEDIATION_PLAYBOOK_CONTRACT.md) |
| KG schema / quality contract | — | [KG_SCHEMA_CONTRACT.md](docs/KG_SCHEMA_CONTRACT.md) |
| Semantic contract | [SEMANTIC_CONTRACT.md](docs/SEMANTIC_CONTRACT.md) | — |
| Audit trail (OTEL) | [AUDIT.md](docs/AUDIT.md) | — |
| DR-план | [DR.md](docs/DR.md) | — |
| Post-mortem 08.08.2026 (исторический) | [POSTMORTEM_2026_08_08.md](docs/POSTMORTEM_2026_08_08.md) | [POSTMORTEM_2026_08_08.ru.md](docs/POSTMORTEM_2026_08_08.ru.md) |
| Базовый срез качества KG 24.05.2026 (исторический) | — | [quality_report_baseline_2026_05_24.md](docs/quality_report_baseline_2026_05_24.md) |
| Golden eval set | [tests/golden/README.md](tests/golden/README.md) | — |
| Changelog | — | [CHANGELOG.md](CHANGELOG.md) |

### sre-ai-copilot и froggy-sre

| | sre-ai-copilot | [froggy-sre](https://github.com/froggychips/froggy-sre) |
|---|---|---|
| **Триггер** | Вебхук Alertmanager (headless) | Вызов MCP-инструмента из Claude Code |
| **Где работает** | Поды Kubernetes | macOS-машина разработчика |
| **Анализ** | Детерминированные правила + KG (LLM-конвейер выключен) | Froggy локально → fallback на Anthropic |
| **k8s-контекст** | In-cluster Kubernetes SDK | `kubectl` через kubeconfig |
| **Хранилище** | PostgreSQL + очередь Celery | `~/.froggy-sre/incidents/` (локальный JSON) |
| **Уведомления** | Discord | Ответ в Claude Code |
| **Когда использовать** | Постоянный headless-алертинг в проде | Интерактивный разбор инцидента через Claude Code |
