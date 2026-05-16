# Module Documentation

## API Layer
- `app/main.py`: FastAPI init, middleware, routers, health/readiness, async job endpoints.
- `app/api/webhooks.py`: AlertManager webhook endpoint and background task status.
- `app/api/replay.py`: Re-run analysis for a historical `incident_id`.
- `app/api/approvals.py`: approve/reject/get for the approval workflow.
- `app/evaluation/feedback.py`: User feedback ingestion and aggregated stats.

## Workers & Orchestration
- `app/workers/tasks.py`: Celery task `process_incident` — full 8-stage agent pipeline including DiagnosticsEngine, MultiHypothesis, FactCritic, Jira enrichment, Fix, Risk, KG update.
- `app/celery_worker.py`: Celery task `generate_reply`, state transitions, iterative confidence loop.
- `app/core/state_machine.py`: Valid states and lifecycle transitions for an incident.

## Diagnostic Engine
- `app/diagnostics/engine.py`: `DiagnosticsEngine` — evaluates all registered rules against k8s context, applies conflict signals via `_apply_conflict_signals()`, returns a populated `FactStore`.
- `app/diagnostics/facts.py`: `FactKind` (canonical slugs), `Fact`, `FactStore`, `MUTUALLY_EXCLUSIVE_PAIRS`, `FactStore.conflicts()`, `FactStore.to_prompt_context()`.
- `app/diagnostics/rules/oom.py`: `OOMKilledRule` — structured gate first (`_check_pod_state()`), text-regex fallback only when target pod has no exit code data; returns `observed=False` if target exit ≠ 0 and ≠ 137.
- `app/diagnostics/rules/crash.py`: `ProcessCrashRule` — detects SIGSEGV/SIGABRT/non-zero exit codes.
- `app/diagnostics/rules/crashloop.py`: `CrashLoopRule` — detects CrashLoopBackOff state.
- `app/diagnostics/rules/scheduling.py`: `FailedSchedulingRule` — detects resource/taint/affinity scheduling failures.
- `app/diagnostics/rules/deploy.py`: `RecentDeployRule` — correlates TeamCity deploy timing with incident.

## Agents
- `app/agents/base.py`: `BaseAgent` — shared `ask()` method wiring LLM backend.
- `app/agents/analyzer.py`: Primary context/incident analysis.
- `app/agents/multi_hypothesis.py`: `MultiHypothesisOrchestrator` — parallel fan-out to perspectives (app/infra/deps/runtime) filtered by `PERSPECTIVE_PRECONDITIONS`; collects `HypothesisResult` with `survived` flag.
- `app/agents/fact_critic.py`: `FactCriticAgent` — adversarial grounding of each hypothesis against `FactStore`; sets `survived=True/False`.
- `app/agents/fix.py`: `FixAgent` — generates `ExecutionIntent` JSON; `_RECURRENCE_PREFIX` for recurrence mode; `_build_jira_prefix()` for Jira enrichment.
- `app/agents/risk.py`: Risk assessment for the proposed remediation.

## Context & Intelligence
- `app/context/context_builder.py`: Assembles and normalises enriched context from all sources.
- `app/context/logs.py`, `metrics.py`, `deployments.py`: Adapters for k8s logs, VM metrics, deploy history.
- `app/context/jira_client.py`: `JiraClient` (Atlassian REST API v3 Basic Auth); `build_jira_context()` → `{open, resolved, has_open, has_resolved, total}` or `None`.
- `app/core/intelligence/similar_incidents.py`: `SimilarIncidentEngine` — KG search with `_is_quality_cause()` filter, `RECURRENCE_WINDOW_DAYS=7`, `recurrence` flag per result.
- `app/core/intelligence/blast_radius.py`, `temporal_diff.py`, `next_steps.py`: Supporting analysis helpers.

## Knowledge Graph

### Schema (`app/knowledge_graph/schema.py`)
- `Service` — nodes (`kg_services`, unique `(namespace, name)`); `synthetic` flag hides infra/observability/drift nodes from KG queries; `team_owner` derived from namespace prefix.
- `ServiceEdge` — directed edges (`kg_service_edges`); `kind` ∈ `calls` / `uses_nats` / `uses_db`; `last_seen_at` for TTL/decay; `extras.discovery_sources` (list) tracks all source flows (multi-source = higher confidence).
- `Deployment` — TC build history (`kg_deployments`); records `started_at` from TC API (not `finishDate`), `triggered_by`, status `SUCCESS`/`FAILURE`/etc.
- `AlertEvent` — alerts from AM (`kg_alerts`); idempotent by `fingerprint`; `resolved_at` refreshed by `kg_alerts_resolve_sync`.
- `PodEvent` — k8s Warning events (`kg_pod_events`); idempotent by `event_uid`; `count` accumulates across kubelet retries.

### Sync (auto-populating beat tasks)
- `app/knowledge_graph/kg_sync.py`: `sync_topology()` — hourly `kubectl get deployments -A` → `kg_services` + edges from env-vars (HTTP URLs, NATS clusters) and `secretKeyRef.key` heuristic (DB DSNs without reading secret values).
- `app/knowledge_graph/k8s_events_sync.py`: `sync_all_events()` — every 10 min, `kubectl get events --field-selector type=Warning` → `kg_pod_events` (OOMKilled, BackOff, FailedScheduling, Unhealthy, etc.). Pod-name → service resolution via standard k8s pod-hash pattern regex.
- `app/knowledge_graph/k8s_ingress_sync.py`: `sync_all_ingresses()` — hourly, `kubectl get ingresses -A` → synthetic `ingress:<host>` nodes + `calls` edges to backend services.
- `app/knowledge_graph/alerts_resolve_sync.py`: `run_alerts_resolve_sync()` — every 15 min, compares `kg_alerts.fingerprint` with `GET AM /api/v2/alerts` → marks non-firing as `resolved_at=NOW`. Safety: min 1 active fingerprint (skip on AM-down).
- `app/knowledge_graph/drift_cleanup.py`: `run_drift_cleanup()` — hourly, marks services from namespaces missing in `kubectl get ns` as `synthetic=true` + `metadata.drift_reason`. Safety threshold 20% drift_pct (skip on kubectl failure → empty ns set).

### Population (`app/knowledge_graph/populator.py`)
Idempotent upserts used by all syncs:
- `upsert_service(namespace, name, team_owner, synthetic, metadata)` — idempotent by `(namespace, name)`.
- `upsert_edge(src, dst, kind, discovered_by, extras)` — idempotent by `(src_id, dst_id, kind)`; refreshes `last_seen_at` and merges `discovery_sources` (unique-preserved-order list).
- `record_deployment(service, started_at, sha, buildtype_id, build_number, ...)` — idempotent by `(service_id, buildtype_id, build_number)`.
- `record_alert_event(service, alertname, fingerprint, ...)` — idempotent by `fingerprint`.
- `record_pod_event(service, event_uid, reason, count, ...)` — idempotent by k8s `event_uid`; updates `last_seen` and `count` on re-sync.

### Queries (`app/knowledge_graph/queries.py`)
Read-side API used by enrichment + MCP tools:
- `recent_deploys_for(ns, svc, before, lookback_minutes)` — returns deploy records with `triggered_by` and TC build URL.
- `upstream_of(ns, svc, kinds=None, fresh_only_days=N)` — outgoing edges; `fresh_only_days` filters by `last_seen_at`; result includes `confidence_score` + `confidence_label`.
- `incidents_on(ns, svc, since, until)` — alert events on service in window.
- `nearby_alerts(ns, svc, around, window_minutes)` — alerts on upstream services in time window.
- `recent_pod_events_for(ns, svc, around, window_minutes)` — pod events with `reason`, `count`, `minutes_before`.

### Confidence (`app/knowledge_graph/confidence.py`)
- `confidence_score(extras, last_seen_at)` → [0, 1]. Formula: `base × source_count_mul × freshness_mul`, clamped.
- `confidence_label(score)` → `high`/`medium`/`low` (thresholds 0.7 / 0.4).
- `confidence_badge(score)` → `●●●` / `●●○` / `●○○` (Discord embed rendering).
- LLM-readiness: when LLM pipeline enables, model sees “inferred with env+url confidence 0.7”, not bare “fact”.

### Alert enrichment (`app/services/alert_enrichment.py`)
- `enrich_alert(db, incident)` → `EnrichedContext`. Synchronous, ~5 SQL queries, **no LLM calls**. Path: `/webhooks/alertmanager/enrich-and-forward`.
- Adaptive `effective_at = max(starts_at, now-24h)` — for long-running chronics, queries are anchored at `now`, not the alert's first-fire timestamp (which can be days old).
- `EnrichedContext.primary_hypothesis()` — top-1 observed Fact (deterministic from `_fact_to_short_text`).
- `EnrichedContext.why_this_matters()` — derived priorization signals from `_matter_signals()`: shared dep (`total_inbound > 20`), chronic (`pod_event.count > 1000`), recurrence pattern, `team_owner=platform`.

### CLI tools (`app/scripts/`)
- `backfill_team_owner.py` — one-shot UPDATE of `kg_services.team_owner` for legacy rows.
- `backfill_tc_deploys.py` — extended TC history backfill (default 30 days, limit 1000).
- `cleanup_drift.py` — thin wrapper over `drift_cleanup.py` for manual run / dry-run preview.

## Data & Persistence
- `app/database.py`, `app/db/*`: Engine/session helpers and database integration.
- `app/models/*` and `app/models.py`: Pydantic/ORM domain models.
- `app/repository.py`: CRUD operations for conversations and messages.

## Services & Safety
- `app/services/mcp_client.py`: Client for executing tools on external MCP servers.
- `app/services/teamcity_service.py`: TeamCity integration for deploy analysis via MCP.
- `app/services/approval_manager.py`: Redis-based approval lifecycle.
- `app/services/k8s_guard.py`: Policy check (verb/resource/namespace/body).
- `app/core/execution_dsl.py`: Strongly typed `ExecutionIntent` and kubectl translator.
- `app/services/resilience.py`: Retry/circuit-breaker logic around LLM calls.
- `app/services/discord_service.py`: Discord notification dispatch.
- `app/services/prompt_guard.py`: Prompt injection detection with input length cap.

## Observability
- `app/telemetry.py`, `app/observability/*`: Tracing, AI metrics, structured logging.
- `app/metrics.py`: Prometheus application metrics.
