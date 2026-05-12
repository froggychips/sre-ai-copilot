# SRE AI Copilot — Architecture

## 1. System Overview

The application consists of: an HTTP API (FastAPI), background tasks (Celery), a data layer (PostgreSQL), external MCP servers (integrations), supporting infrastructure (Redis, Discord, Kubernetes), and a fact-anchored diagnostic engine that feeds a multi-hypothesis LLM pipeline.

## 2. Runtime Components

- **API app (`app.main`)**: routing, auth dependency, metrics middleware, health/readiness endpoints.
- **Webhook pipeline (`app.api.webhooks` + `app.workers.tasks`)**: receives AlertManager payloads, builds context, runs the agent pipeline.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: background conversational analysis with a confidence-threshold loop.
- **DiagnosticsEngine (`app.diagnostics.engine`)**: rule-based evaluation producing a typed `FactStore`.
- **FactStore (`app.diagnostics.facts`)**: canonical ground-truth about the incident; supports conflict detection and prompt serialization.
- **MultiHypothesisAgent (`app.agents.multi_hypothesis`)**: fan-out to 4 perspectives (app/infra/deps/runtime) with precondition filtering.
- **FactCriticAgent (`app.agents.fact_critic`)**: adversarial grounding — eliminates hypotheses that contradict observed facts.
- **FixAgent (`app.agents.fix`)**: generates a structured `ExecutionIntent`; recurrence-aware and Jira-enriched.
- **SimilarIncidentEngine (`app.core.intelligence.similar_incidents`)**: KG-based recurrence detection (7-day window).
- **JiraClient (`app.context.jira_client`)**: Atlassian REST API enrichment for FixAgent context.
- **Data layer (`app.database`, `app.repository`)**: SQLAlchemy models and CRUD operations.
- **Integration layer (`app.services.mcp_client`)**: MCP client for k8s, TeamCity, and other external tools.
- **Safety services**: approval manager, K8s guard, execution DSL.

## 3. Data Flow (Webhook Incident Pipeline)

```
AlertManager webhook
  → POST /webhooks/alertmanager
  → for each alert: IncidentRecord(status=PENDING)
  → Celery task: process_incident

  Stage 1: Context Builder
    → k8s_pod_state (live pod metadata)
    → vm_metrics (VictoriaMetrics memory/CPU)
    → teamcity_context (MCP — recent deploys)

  Stage 2: DiagnosticsEngine
    → rules: OOMKilledRule, ProcessCrashRule, CrashLoopRule, …
    → produces: FactStore{oom_killed, process_crash, crashloop, …}
    → conflict detection: MUTUALLY_EXCLUSIVE_PAIRS → cap confidence

  Stage 3: MultiHypothesisAgent
    → PERSPECTIVE_PRECONDITIONS filter (runtime requires process_crash)
    → parallel LLM fan-out per active perspective
    → FactCriticAgent grounding → survivors

  Stage 4: Jira Enrichment (best-effort)
    → JiraClient.search_by_service(service, namespace)
    → build_jira_context() → {open, resolved, has_open}

  Stage 5: FixAgent
    → recurrence-aware (_RECURRENCE_PREFIX if is_recurrence)
    → Jira-enriched (_build_jira_prefix if jira_context)
    → generates ExecutionIntent JSON

  Stage 6: RiskAgent → Discord approval
  Stage 7: IncidentRecord(status=COMPLETED, analysis=…)
  Stage 8: KG update (_is_quality_cause filter)
```

## 4. Fact-Anchored Reasoning

The diagnostic engine runs before any LLM agent. It evaluates deterministic rules against structured k8s data (`k8s_pod_state`, pod events) and emits a `FactStore` — a typed collection of `Fact` objects, each with:

- `kind`: canonical slug (`FactKind.OOM_KILLED`, `FactKind.PROCESS_CRASH`, …)
- `observed`: whether the fact was confirmed
- `confidence`: 0.0–1.0 (capped on conflict)
- `evidence`: dict of supporting data

`FactStore.conflicts()` detects `MUTUALLY_EXCLUSIVE_PAIRS` (e.g. `{oom_killed, process_crash}`) and `_apply_conflict_signals()` caps confidence to 0.60, adds `conflict_with` to evidence, and appends a `<conflicts>` block to the prompt context visible to all agents.

LLM agents receive the FactStore serialized as a `<facts>` XML block. The FactCriticAgent uses it to adversarially ground each hypothesis — a hypothesis that contradicts confirmed facts is rejected.

## 5. Data Flow (Copilot Conversation)

```
POST /copilot
  → conversation persistence
  → Celery task: generate_reply
  → state: INVESTIGATING
  → context builder
  → up to 3 analysis iterations (confidence threshold 0.7)
  → states: HYPOTHESIS_GENERATED → FIX_PROPOSED
  → optional Discord notification
```

## 6. Recurrence Detection

`SimilarIncidentEngine` queries the KG for past incidents for the same service where:
- `resolution_quality = "resolved"` (quality-gate: `_is_quality_cause` passed)
- `resolved_at >= now() - RECURRENCE_WINDOW_DAYS` (default: 7 days)

If a match is found, `recurrence=True` is set in the pipeline output. `FixAgent` switches to investigative mode (`_RECURRENCE_PREFIX`) — it will NOT recommend a simple restart because that has already been tried.

## 7. Reliability & Observability

- Celery retries are configured on critical tasks.
- `/readyz` checks PostgreSQL availability with `SELECT 1`.
- Prometheus latency metrics are collected by the request middleware.
- OpenTelemetry tracing is initialized at startup (exports to Tempo if available).
- Structured audit log via `structlog` (stdout for production, file for local dev).

## 8. Security

- JWT-based user dependency for `/copilot`.
- `ALERTMANAGER_WEBHOOK_SECRET` HMAC validation for webhook endpoints.
- Guardrails at the DSL level (`ExecutionIntent`) and k8s policy validator (`K8sSecurityGuard`).
- Approval API for human-in-the-loop before any write action.
- `SAFE_MODE=true` is enforced in production (config validator raises on `SAFE_MODE=false` + `ENV=production`).
- Prompt injection guard (`prompt_guard.detect_injection`) with input length cap (`PROMPT_INPUT_MAX_CHARS`).

## 9. External Integrations

| Integration | Purpose | Config keys |
|---|---|---|
| Kubernetes (MCP) | Pod state, logs, events, deployment control | `TEAMCITY_MCP_URL` (via wo-tools) |
| VictoriaMetrics | Memory/CPU metrics window before incident | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Recent deploy context | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Known open/resolved tickets for the service | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Approval flow + incident report | `DISCORD_WEBHOOK_URL` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
