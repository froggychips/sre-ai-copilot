# Module Documentation

## API Layer
- `app/main.py`: FastAPI init, middleware, routers, health/readiness, async job endpoints.
- `app/api/webhooks.py`: AlertManager webhook endpoint and background task status.
- `app/api/replay.py`: Re-run analysis for a historical `incident_id`.
- `app/api/approvals.py`: approve/reject/get for the approval workflow.
- `app/evaluation/feedback.py`: User feedback ingestion and aggregated stats.

### Discord interactions (`app/api/discord_interactions.py`)
- Discord application-interactions endpoint — receives Approve / Decline button clicks from the incident embed.
- **Authorization gate (fail-closed):**
  - Allow if `interaction.user.id ∈ DISCORD_APPROVERS_USER_IDS`, OR
  - Allow if `member.roles ∩ DISCORD_APPROVERS_ROLE_IDS ≠ ∅`.
  - Otherwise — ephemeral deny + audit log `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
  - If both allowlists are empty: deny with `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`. The system never auto-allows by absence of policy.
- **Rate-limit:** in-memory, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` per Discord user (default 5). An authz failure does NOT consume quota — a blocked user can't be used to exhaust the limit for a legitimate approver.
- **Persistence:** on accept, writes one `ActionApproval` row (UNIQUE `(incident_id, intent_signature)`). Concurrent clicks collide on the unique key and the second click gets a "already approved/declined by @user" ephemeral.

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

### Metrics sync (`app/knowledge_graph/metrics_sync.py`)
- `sync_service_health()` — every 5 min, runs five PromQL queries per service (`cpu_pct`, `mem_pct`, `restarts_rate`, `http_5xx_rate`, `p95_latency_ms`) against VictoriaMetrics and upserts into `kg_service_health`.
- Idempotency: each row insert is wrapped in a Postgres `SAVEPOINT`; an `IntegrityError` on the unique `(service_id, ts)` key rolls back to the savepoint and the loop continues. Re-running on overlapping windows is safe.
- **Wave 5 gotcha (read this before editing the queries):** `mem_pct` must be `avg(rate(working_set) / on(pod) container_limit)` — divide first, then aggregate. The aggregate-then-divide form silently returns 0 because the many-to-one join collapses before the division. The `kg_self_health` canary `materialization_zero_rate` was added specifically to detect this regression class.
- Tunable via env vars (VM query window, lookback, per-service timeout). When VM is unreachable the task no-ops (next tick retries); see `kg_self_health.sync_lag` for the dropped-tick signal.

### Cluster health sync (`app/knowledge_graph/cluster_health_sync.py`)
- `sync_cluster_observations()` — every 5 min, snapshots k8s node-level state (Ready/SchedulingDisabled, allocatable vs requested CPU/mem, pod count) into `kg_cluster_observations`. Source of truth for the cluster-wide capacity view.

### Ingress observations sync (`app/knowledge_graph/ingress_observations_sync.py`)
- `sync_ingress_observations()` — every 5 min, ingress-controller PromQL (`nginx_ingress_controller_*`) → `kg_ingress_observations` per host.
- Honest status today: the schema and the sync are live, but the scrape config that would expose ingress metrics for application namespaces is not in place — so `http_5xx_rate` and `p95_latency_ms` come out as 0. Downstream consumers (anomaly, digest) treat this as no-signal rather than as healthy.

### Anomaly detection (`app/knowledge_graph/anomaly_detection.py`)
- `scan_anomalies()` — every 5 min, reads `kg_service_health` (and `kg_ingress_observations` where populated) and writes `kg_anomaly_observations` rows for each `(service, metric)` whose current value is statistically off the baseline.
- **Method:** rolling robust-z, `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Robust to outliers in the baseline window itself.
- **Seasonal baseline:** when ≥50 historical points are available, the baseline is stratified by hour-of-day. `extras.method = robust_z_seasonal`. Otherwise `robust_z_flat`.
- **Thresholds:** `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) → `warning` row, `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`) → `critical`.
- **Volume guard:** at most 3 observations per (service, metric) per hour. Prevents flooding when a metric stays anomalous.
- **Baseline window:** 7 days rolling.
- `flat_baseline` is recorded in `extras` when the baseline is all zeros — used downstream by the deploy correlator to damp confidence (a "spike from 0" on a dead metric isn't a signal).

### Deploy correlator (`app/rca/deploy_correlator.py`)
- Invoked inline by `IncidentPipeline._enrich_deploy_correlation` for every incident that reaches enrichment.
- **Window:** `[incident_started_at − 2h, incident_started_at]`. Every `kg_deployment` of the affected service in that window is a candidate cause.
- **Confidence formula** — weighted multi-factor score in `[0, 1]`:
  - `N_spikes` — number of anomaly observations of the service after the deploy
  - `max_zscore` — peak robust-z observed in the window
  - `time_proximity` — closer deploys score higher
  - `deploy_status_factor` — FAILED deploys carry higher prior than SUCCESS
  - `flat_baseline_penalty` — dampens confidence when baseline was all zeros
- **Verdict tiers:** `likely` (≥ 0.7) / `suspect` (0.4–0.7) / `weak` (0.2–0.4) / `unlikely` (< 0.2).
- **Tuning:** thresholds are constants in the module; for ad-hoc tuning, run a backfill on past incidents and inspect `analysis["deploy_correlator"]["confidence"]` and `["factors"]` to see which factor dominated.

### Seq logs sync (`app/knowledge_graph/seq_logs_sync.py`)
- `sync_logs()` — every 10 min, pulls log events from the Seq REST API for the last 10-minute window and upserts one row per `(service, level)` into `kg_log_observations` (count + redacted sample).
- **Match algorithm:** Seq event `Application` / `Service` property → `kg_services.name`; fallback to namespace prefix.
- **PII redaction integration:** every captured `message_sample` is passed through `app/services/pii_redaction.py` *before* write, then truncated to 500 chars. The DB never stores raw PII.
- **Known limitation:** with the current realm layout, all events come in as `Information`. Per-realm Seq instances or access-log routing are required to populate `Error`/`Warning` independently. The schema and downstream consumers are already level-aware.

### Signal aggregates (`app/knowledge_graph/signal_aggregates.py`)
- `aggregate_signals()` — hourly roll-up over a 24h window. Writes one row per service into `kg_signal_aggregates` with five metrics:
  - anomaly count by severity
  - deploy count (SUCCESS / FAILURE)
  - alert event count
  - pod event count
  - log error rate
- Consumed by the daily team digest (fragile top-5 ranking) and by `health_score.py` (per-service composite).

### Self-health (`app/knowledge_graph/self_health.py`)
"Monitoring of the monitoring." Six canaries against KG data quality, run every 30 minutes by the `kg_self_health_check` beat task.

| Check | Pass condition |
|---|---|
| `materialization_zero_rate` | <X% rows with value = 0/NULL per metric in `kg_service_health` over 24h. Allowlist for known-zero metrics. |
| `sync_lag` | Per beat task, `max(ts)` within 2× expected interval. |
| `anomaly_signal_health` | Anomaly observation count over 24h in a sane band (not 0, not >500). |
| `alerts_resolve_freshness` | Open-and-old `kg_alerts` count below threshold. |
| `pod_events_link_rate` | ≥80% of `kg_pod_events` have a resolved `service_id`. |
| `edges_freshness` | ≤30% of `kg_service_edges` are stale (`last_seen_at` >24h or NULL). |

- Output: audit log (`KG_SELF_HEALTH_OK` / `_WARN` / `_FAIL`) and, on any `fail`/`warn`, a single embed posted to `DISCORD_WEBHOOK_SELF_HEALTH_URL` (kept separate from `#infra-error`).
- **Dedup:** 6-hour window per check at the beat-task level — the same failing canary won't repost until it has changed status or 6h have elapsed.
- **Adding a new check:** add a `_check_X(db) -> CheckResult` function and append it to the `ALL_CHECKS` list. Each check is read-only and self-contained.
- **Configuration:** thresholds are constants in the module; the Discord webhook is taken from `settings.DISCORD_WEBHOOK_SELF_HEALTH_URL` (None disables Discord output but keeps the audit log).

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
- `app/services/discord_service.py`: Discord notification dispatch. Renders incident embeds, manages 30-min dedup (PATCH via `webhook?wait=true`), severity routing, per-team channel routing (`DISCORD_TEAM_CHANNEL_MAP`), suspect-deploy / log-error / anomaly / recurrence / linked-alerts blocks, and Approve/Decline action rows.
- `app/services/prompt_guard.py`: Prompt injection detection with input length cap.

### PII redaction (`app/services/pii_redaction.py`)
- Pure regex-based redactor. Patterns: email addresses, IPv4, IPv6, JWT (`xxx.yyy.zzz` base64), `Bearer …` headers, UUIDs, long hex strings, and `password|token|secret|api_key` key/value pairs.
- **Write-time application** in `seq_logs_sync.py` — log lines are redacted before being persisted into `kg_log_observations.message_sample`. The DB stores no raw PII. Output is truncated to 500 chars after redaction.
- **Defense-in-depth** at embed render in `discord_service.py` — every string heading to a webhook is run through the redactor a second time, on the assumption that any future data source might bypass write-time scrubbing.
- **Idempotency:** running the redactor on already-redacted text produces the same output.

### Approval manager (`app/services/approval_manager.py`)
- Redis-based approval lifecycle (existing). Now backed by a persistent `ActionApproval` ORM row (`kg_action_approvals`, see `app/knowledge_graph/schema.py`) with `UNIQUE (incident_id, intent_signature)`.
- `intent_signature` is a deterministic hash of `ExecutionIntent` (action + resource + namespace + params), computed by `app/services/intent_signature.compute_signature`. One action = one approval row; a repeat click on the same embed hits the UNIQUE constraint and the handler responds "already approved/declined by @user".
- Status is final on creation (`approved` | `declined`); there is no `pending` row — either the button was clicked or it wasn't.

## Observability
- `app/telemetry.py`, `app/observability/*`: Tracing, AI metrics, structured logging.
- `app/metrics.py`: Prometheus application metrics.
