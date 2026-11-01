# Module Documentation

## API Layer
- `app/api/incidents.py`: `GET /kg/incidents` (`status`, `namespace`, `service`, `include_noise`; noise hidden by default), `GET /kg/incidents/{id}` (with alerts), `GET /kg/incidents/{id}/timeline` (`incident_timeline.build_timeline`). Mounted with router-level `get_current_user`.
- `app/api/kg_graph.py`: `GET /kg/blast-radius?namespace=&service=&hops=&limit=&include_inactive=` → `blast_radius.blast_radius_v2`; 404 when the target is not in the graph.
- `app/main.py`: FastAPI init, middleware, routers, health/readiness, async job endpoints.
- `app/api/webhooks.py`: Alertmanager receivers — `/webhooks/alertmanager/enrich-and-forward` (KG + enriched Discord card; prod, preprod, nodes), `/webhooks/alertmanager/store` (KG only; squads, preupdate, info), `/webhooks/alertmanager` (LLM pipeline, not routed in production) — and background task status. Both production paths only enqueue the remediation bridge (`bridge_dispatch`).
- `app/api/replay.py`: Re-run analysis for a historical `incident_id`.
- `app/api/approvals.py`: approve/reject/get for the approval workflow.
- `app/evaluation/feedback.py`: User feedback ingestion and aggregated stats.

### Discord interactions (`app/api/discord_interactions.py`)
- Discord application-interactions endpoint (`POST /discord/interactions`) — receives `approve:` / `decline:` button clicks from the incident card (remediation bridge or LLM pipeline). In production the AI Bot application's Interactions Endpoint points at a separate service that relays these clicks to the copilot (`DISCORD_INTERACTIONS_COPILOT_RELAY_URL` on that side) and returns the copilot's response body; the Ed25519 signature is verified here (`DISCORD_PUBLIC_KEY`).
- **Authorization gate (fail-closed):**
  - Allow if `interaction.user.id ∈ DISCORD_APPROVERS_USER_IDS`, OR
  - Allow if `member.roles ∩ DISCORD_APPROVERS_ROLE_IDS ≠ ∅`.
  - Otherwise — ephemeral deny + audit log `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
  - If both allowlists are empty: deny with `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`. The system never auto-allows by absence of policy.
- **Rate-limit:** in-memory, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` per Discord user (default 5). An authz failure does NOT consume quota — a blocked user can't be used to exhaust the limit for a legitimate approver.
- **Persistence:** on accept, writes one `ActionApproval` row (UNIQUE `(incident_id, intent_signature)`). Concurrent clicks collide on the unique key and the second click gets a "already approved/declined by @user" ephemeral.

## Workers & Orchestration
- `app/workers/tasks.py`: Celery app, beat schedule and tasks. `process_incident` — full 8-stage agent pipeline (DiagnosticsEngine, MultiHypothesis, FactCritic, Jira enrichment, Fix, Risk, KG update); exits without LLM calls while `LLM_PIPELINE_ENABLED=false` (production). `remediation_bridge` — the deterministic bridge (queue `celery`); `remediation_verify` — post-apply verification. Heavy KG syncs (`SYNC_QUEUE_TASKS`) are routed to the `sync` queue (`copilot-worker-sync`), the rest to `celery`. Calls `configure_logging()` at import (all Celery processes).
- `app/workers/executor_tasks.py`: `executor_dry_run` / `executor_apply` in the `executor` queue (`EXECUTOR_QUEUE_NAME`) for `EXECUTOR_DISPATCH=queue` — the only process with a write ServiceAccount (`copilot-executor`).
- `app/workers/task_lock.py`: singleton locks for beat tasks (no second instance on top of a running one).
- `app/workers/pipeline_scope.py`: which alerts may reach the LLM pipeline at all (severity / namespace scope).
- `app/workers/pipeline.py`: `IncidentPipeline` — the stages themselves, one async method each, resumable from a checkpoint after a Celery retry.
- `app/workers/report_delivery.py`: `ReportDelivery` — Discord report outbox (`report_pending` / `report_sent` / `report_failed` markers, attempt counter, retry-or-give-up decision). Deliberately not a pipeline stage: it runs *after* the terminal state is committed, survives worker death and is retried by Celery without re-running any LLM stage.
- `app/celery_worker.py`: Celery task `generate_reply`, state transitions, iterative confidence loop.
- `app/core/state_machine.py`: Valid states and lifecycle transitions for an incident.

## Diagnostic Engine
- `app/diagnostics/engine.py`: `DiagnosticsEngine` — evaluates all registered rules against k8s context, applies conflict signals via `_apply_conflict_signals()`, returns a populated `FactStore`.
- `app/diagnostics/facts.py`: `FactKind`, `Fact`, `FactStore`, `Verdict`. **Evidence contract (v1.0.4)**: `Fact.verdict` ∈ `found` / `absent` / `unknown`; `Fact.unknown(kind, reason)` is the only way to produce `unknown` (confidence forced to 0, `epistemic=unknown`, `unknown_reason` mandatory); `epistemic` / `provenance` / `window_min` / `data_age_min`; `FactStore.unknown_kinds()`; `to_prompt_context()` renders `✓ / ✗ / ?` and warns the critic that `?` is not a refutation.
- `app/diagnostics/rules/base.py`: `Rule.sources` (ctx fields the rule depends on) and `Rule.run()` = `evaluate()` + demotion of `absent` → `unknown` when a declared source is listed in `ctx["source_status"]`; `Rule.source_problem()`. Guard: `tests/test_evidence_contract.py`.
- `app/diagnostics/rules/oom.py`: `OOMKilledRule` — structured gate first (`_check_pod_state()`), text-regex fallback only when target pod has no exit code data; returns `observed=False` if target exit ≠ 0 and ≠ 137.
- `app/diagnostics/rules/process_crash.py`: `ProcessCrashRule` — detects SIGSEGV/SIGABRT/non-zero exit codes.
- `app/diagnostics/rules/crashloop.py`: `CrashLoopRule` — detects CrashLoopBackOff state.
- `app/diagnostics/rules/failed_scheduling.py`: `FailedSchedulingRule` — detects resource/taint/affinity scheduling failures.
- `app/diagnostics/rules/recent_deploy.py`: `RecentDeployRule` — correlates TeamCity deploy timing with incident.

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

## Knowledge Graph

### Schema (`app/knowledge_graph/schema.py`)
- `Service` — nodes (`kg_services`, unique `(namespace, name)`); `synthetic` flag hides infra/observability/drift nodes from KG queries; `team_owner` derived from namespace prefix; `stale_class` (PR #86): `active` / `expected_stale` / `suspicious_stale` (см. `stale_classifier`).
- `Service.node_kind` — the node's role: `service` (k8s Service / logical entry point), `workload` (Deployment/StatefulSet/DaemonSet), `ingress` (synthetic). Unique key is `(namespace, name, node_kind)`. Looking a node up by `(namespace, name)` alone is ambiguous: `.one_or_none()` will raise `MultipleResultsFound`.
- `ServiceEdge` — directed edges (`kg_service_edges`); `kind` ∈ `calls` / `uses_nats` / `uses_db` / `serves_traffic` / `routes_to`; `last_seen_at` for TTL/decay; `extras.discovery_sources` (list) tracks all source flows (multi-source = higher confidence).
- `Deployment` — TC build history (`kg_deployments`); records `started_at` from TC API (not `finishDate`), `triggered_by`, status `SUCCESS`/`FAILURE`/etc.
- `AlertEvent` — alerts from AM (`kg_alerts`); idempotent by `fingerprint`; `resolved_at` refreshed by `kg_alerts_resolve_sync`.
- `PodEvent` — k8s Warning events (`kg_pod_events`); idempotent by `event_uid`; `count` accumulates across kubelet retries.
- `K8sJob` (PR #82, `kg_k8s_jobs`) — Job/CronJob узлы вне `kg_services`; `kind` ∈ `job` / `cronjob`; `owner_service_id` метаколонка реализует semantic edge `runs_as_job` без отдельного edge-row.
- `StorageVolume` (PR #84, `kg_storage_volumes`) — PVC/PV узлы; `kind` ∈ `pvc` / `pv` (PVC — namespace-scoped, PV — cluster-scoped с `namespace=''`); атрибуты: phase, capacity_bytes, storage_class.
- `VolumeEdge` (PR #84, `kg_volume_edges`) — heterogeneous edges (`uses_volume` Service→PVC, `bound_to` PVC→PV); tagged `src_kind`/`dst_kind` без FK constraint.
- `IngressObservation` (`kg_ingress_observations`) — per-endpoint (host/path) HTTP snapshots: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_5xx_rate` / `error_4xx_rate`, `service_id` FK to backend; `UNIQUE (ingress_name, host, path, ts)`. Populated since 2026-06-10 (see Ingress observations sync).

### Schema / quality contract (`app/knowledge_graph/contract.py`)
- `KG_SCHEMA_VERSION` — current version (`2.9`, namespace owner `kg_namespaces.owner_*`; history in the module and in `docs/KG_SCHEMA_CONTRACT.md`). Bump rules — `docs/KG_SCHEMA_CONTRACT.md` §8.
- `EDGE_KINDS` — реестр всех edge kinds + spec (`semantic` / `src_kinds` / `dst_kinds` / `source` / `status` / `table`). `table` = где edge живёт: `kg_service_edges` / `kg_volume_edges` / `fk_only` (через FK) / `metadata_only` (через owner_service_id).
- `OWNER_SOURCES` / `OWNER_SOURCE_ALIASES` — canonical источники owner-а + маппинг коротких имён из `ownership_suggester` (`prefix`→`namespace_prefix`, `labels`→`k8s_labels` и т.д.).
- `STALE_CLASS_VALUES` — enum значений `kg_services.stale_class`. Re-export'ится в `stale_classifier` для backward-compat.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика: сверяет реальные kinds в БД с реестром, логирует drift.
- Test: `tests/test_contract_drift.py` (Gate #22) — auto-validation что код и контракт не разъезжаются.

### Sync (auto-populating beat tasks)
- `app/knowledge_graph/kg_sync.py`: `sync_topology()` — hourly `kubectl get deployments -A` → `kg_services` + edges from env-vars (HTTP URLs, NATS clusters) and `secretKeyRef.key` heuristic (DB DSNs without reading secret values).
- `app/knowledge_graph/k8s_events_sync.py`: `sync_all_events()` — every 10 min, `kubectl get events --field-selector type=Warning` → `kg_pod_events` (OOMKilled, BackOff, FailedScheduling, Unhealthy, etc.). Pod-name → service resolution via standard k8s pod-hash pattern regex.
- `app/knowledge_graph/k8s_ingress_sync.py`: `sync_all_ingresses()` — hourly, `kubectl get ingresses -A` → synthetic `ingress:<host>` nodes + `calls` edges to backend services.
- `app/knowledge_graph/alerts_resolve_sync.py`: `run_alerts_resolve_sync()` — every 15 min, compares `kg_alerts.fingerprint` with `GET AM /api/v2/alerts` → marks non-firing as `resolved_at=NOW`. Safety: min 1 active fingerprint (skip on AM-down).
- `app/knowledge_graph/drift_cleanup.py`: `run_drift_cleanup()` — hourly, marks services from namespaces whose absence is confirmed twice (missing in `kubectl get ns` and `kg_namespaces.state='missing'` for more than 24 h) as `synthetic=true` + `metadata.drift_reason` / `drift_marked_at` / `drift_prev_synthetic`. Safety: no-op if the live namespace set shrank by more than 20%. Since 1.0.33 `_unmark_revived` removes the mark from nodes of a namespace that is back (in `kubectl get ns` and `active` in `kg_namespaces`) and restores the previous `synthetic`; stats `unmarked_services`.
- `app/knowledge_graph/k8s_jobs_sync.py` (PR #82): `sync_jobs_and_cronjobs()` — hourly, `kubectl get jobs,cronjobs -A -o json` → `kg_k8s_jobs` rows. Owner Service резолвится через label `app.kubernetes.io/part-of` (fallback `app`) → semantic `runs_as_job` edge через `owner_service_id` metadata-column (а не отдельный edge-row в `kg_service_edges`).
- `app/knowledge_graph/k8s_storage_sync.py` (PR #84): `sync_storage()` — every 30 min, отдельные проходы для PV (cluster-scoped), PVC (namespace-scoped) + scan pod.spec.volumes для `uses_volume` edges. Все edges идут в `kg_volume_edges` (heterogeneous, tagged src/dst). Опциональный disk_pct enrichment через `kubelet_volume_stats_*` PromQL (default OFF — scrape config redirect нужен).
- `app/knowledge_graph/stale_classifier.py` (PR #86): `classify_stale_with_deploys(name, ns, last_deploy_at, team_owner)` → `active` / `expected_stale` / `suspicious_stale`. Используется `kg_sync.sync_namespace` для апдейта `kg_services.stale_class` идемпотентно. Re-exports canonical values из `contract.STALE_CLASS_VALUES`.

### Population (`app/knowledge_graph/populator.py`)
Idempotent upserts used by all syncs:
- `upsert_service(namespace, name, team_owner, synthetic, metadata)` — idempotent by `(namespace, name, node_kind)`. Since 1.0.33 the row is rewritten only if a field actually changes or `updated_at` is older than `SERVICE_TOUCH_INTERVAL` (30 min).
- `upsert_edge(src, dst, kind, discovered_by, extras)` — idempotent by `(src_id, dst_id, kind)`; merges `discovery_sources` (unique-preserved-order list). Since 1.0.33 `last_seen_at` is touched only when older than `EDGE_TOUCH_INTERVAL` (3 h) or `weight` grows.
- `record_deployment(service, started_at, sha, buildtype_id, build_number, ...)` — idempotent by `(service_id, buildtype_id, build_number)`; an existing row is updated only to backfill `sha` (1.0.33).
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
- `backfill_tc_deploys.py` — extended TC history backfill (default 30 days, limit 1000).
- `cleanup_drift.py` — thin wrapper over `drift_cleanup.py` for manual run / dry-run preview.
- `reattribute_deploys.py` — re-attributes ns-broadcast `kg_deployments` rows to exact targets from TeamCity build properties (`--apply --limit-builds N`).
- `reattribute_alerts.py` — re-attributes historical alerts to the k8s object via `alert_target` (labels from VictoriaMetrics by fingerprint; `--days N`, dry-run by default, `--apply`).
- `reattribute_job_alerts.py` — moves `KubeJobFailed` / `KubeJobNotCompleted` alerts from `vm-kube-state-metrics` to the Job/CronJob owner (`job_attribution`), shrinks or deletes the emptied KSM incidents and attaches the alerts to the owner's incident; dry-run by default, `--apply` to move.

### Metrics sync (`app/knowledge_graph/metrics_sync.py`)
- `sync_service_health()` — every 5 min, runs five PromQL queries per service (`cpu_pct`, `mem_pct`, `restarts_rate`, `http_5xx_rate`, `p95_latency_ms`) against VictoriaMetrics and upserts into `kg_service_health`.
- Idempotency: each row insert is wrapped in a Postgres `SAVEPOINT`; an `IntegrityError` on the unique `(service_id, ts)` key rolls back to the savepoint and the loop continues. Re-running on overlapping windows is safe.
- **Wave 5 gotcha (read this before editing the queries):** `mem_pct` must be `avg(rate(working_set) / on(pod) container_limit)` — divide first, then aggregate. The aggregate-then-divide form silently returns 0 because the many-to-one join collapses before the division. The `kg_self_health` canary `materialization_zero_rate` was added specifically to detect this regression class.
- Tunable via env vars (VM query window, lookback, per-service timeout). When VM is unreachable the task no-ops (next tick retries); see `kg_self_health.sync_lag` for the dropped-tick signal.
- **Zero semantics (still true as of 2026-06-10):** `http_5xx_rate` and `p95_latency_ms` are always 0 — the ASP.NET services' `/metrics` endpoints are behind JWT (401), so there is no app-level scrape. Backend ticket **WO-12483**; once rolled out, a `VMServiceScrape` on the app namespaces will bring these fields to life. Until then `0` = "no data", NOT "no errors / fast". Per-host/path HTTP signal lives in `kg_ingress_observations` instead (populated since 2026-06-10, see below); ingress metrics are no longer the missing piece here.

- **Здоровье Orleans-силоса (v1.0.9)**: discovery `_q_orleans_namespaces()` + семь запросов `_q_ns_orleans_*` на Orleans-namespace, все только по pull-сериям (`job=~".+/.+"`, `_ORLEANS_PULL`; пуш-копии в push-gateway отсекаются) → `orleans_latency_avg_ms` (взвешенная по вызовам), `orleans_timedout_rate`, `orleans_messaging_fault_rate` (rejected+expired+sent_failed+sent_dropped, без rerouted), `orleans_pings_missed_rate`, `orleans_activation_churn`, `orleans_rerouted_rate` (в минуту; рераны отдельно от сбоев с 1.0.11); `_orleans_metrics()` превращает отсутствующие серии сбоев в `0.0`, если есть `latency_count`. Stats: `orleans_namespaces`, `orleans_queries`.
### Cluster health sync (`app/knowledge_graph/cluster_health_sync.py`)
- `sync_cluster_observations()` — every 5 min, snapshots k8s node-level state (Ready/SchedulingDisabled, allocatable vs requested CPU/mem, pod count) into `kg_cluster_observations`. Source of truth for the cluster-wide capacity view.

### Ingress observations sync (`app/knowledge_graph/ingress_observations_sync.py`)
- `sync_ingress_observations()` — every ~10 min (beat task `kg_ingress_observations_sync`), ingress-controller PromQL (`nginx_ingress_controller_*`) → `kg_ingress_observations`, one row per `(ingress_name, host, path)` with `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_5xx_rate` / `error_4xx_rate`. Idempotent by `UNIQUE (ingress_name, host, path, ts)`.
- **Populated since 2026-06-10:** controller metrics are enabled on both nginx-ingress DaemonSets of the cluster and scraped via a `VMPodScrape` with `honorLabels`. 100% of rows are linked to `kg_services` through `service_id` (backend resolved from the ingress rule).
- **Zero semantics:** `error_5xx_rate = 0` alongside non-zero `rps`/`p95` genuinely means "no errors" — the data is there. Rows where ALL metrics are 0 are skipped by the sync (exporter doesn't cover the endpoint), so "no data" shows up as an absent row, not as zeros.
- **Granularity:** endpoint-level (host/path), NOT a per-service aggregate — do not conflate with `kg_service_health` (whose `http_5xx_rate`/`p95_latency_ms` are still always 0, see Metrics sync above).

### Anomaly detection (`app/knowledge_graph/anomaly_detection.py`)
- `scan_anomalies()` — every 5 min, reads `kg_service_health` and `kg_ingress_observations` (populated since 2026-06-10) and writes `kg_anomaly_observations` rows for each `(service, metric)` whose current value is statistically off the baseline. Note: app-level `http_5xx_rate`/`p95_latency_ms` in `kg_service_health` are still always 0 (app `/metrics` behind JWT, WO-12483), so app-layer anomalies come from `log_error_rate` (a log-derived proxy, NOT HTTP 5xx) and from ingress observations.
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

### Wave 7-X: declarative k8s topology resources (`app/knowledge_graph/k8s_topology_resources_sync.py`)
- `sync_topology_resources(db)` — main entry point. Every 15 minutes the beat task
  `kg_topology_resources_sync` runs `kubectl get services -A -o json` and
  `kubectl get ingresses -A -o json`, upserts `kg_services` with
  `metadata_json` (service_type, ports, selector) and writes two edge kinds.
- **Edge `serves_traffic`** (`EDGE_SERVES_TRAFFIC`): Service → backing
  **workload**. Algorithm: selector-match of Service.spec.selector against the pod
  template labels of workloads in the same namespace; Deployment,
  **StatefulSet and DaemonSet** are matched (without the last two, 2231 Services
  per tick fell into `skipped_no_match` — all the `*-db` / `*-postgresql` /
  clickhouse ones). The workload is a separate node `node_kind='workload'`; the sync
  creates it itself and inherits `team_owner` from the Service. Before contract 2.4
  a Service and a Deployment with the same name were ONE row, and this edge
  degenerated into a self-loop: 2092 dropped edges per tick with 3 left in the graph.
  A Service without a match is still registered as a node (a downstream Ingress may
  route to it).
- **Batched commits** (`_COMMIT_BATCH = 200`): a single transaction for 4200+
  upserts lived 12–13 minutes holding ACCESS SHARE the whole time, so a DDL
  migration queued behind it and blocked readers. The tick is deliberately no longer
  atomic — the sync is idempotent.
- **Edge `routes_to`** (`EDGE_ROUTES_TO`): Ingress (as a resource) → backend
  Service. A synthetic node `ingress:<name>` is created if missing. Parallel slice to
  `k8s_ingress_sync` (which builds `ingress:<host>` → backend as `calls`) —
  Ingress-as-resource vs Ingress-as-host. Merged through `populator.upsert_edge`
  (`extras.discovery_sources`).
- **Cluster-wide RBAC**: needs `services`, `ingresses`, `deployments`,
  `statefulsets`, `daemonsets` with verbs `get`/`list`/`watch` in the ClusterRole
  (see `k8s/base/rbac.yaml`).
- **No feature flag** — declarative, idempotent, no external dependencies besides
  `kubectl` in the worker pod.
- **Failure mode**: `subprocess.TimeoutExpired` or a non-zero kubectl exit —
  logs.warning, the beat tick returns an empty result, the next tick retries.
- CLI: `python -m app.knowledge_graph.k8s_topology_resources_sync [namespace]`
  (one ns or all).

### Wave 7-Y: PodEvent runtime correlation (`app/knowledge_graph/runtime_correlation.py`)
- `run_runtime_correlation_sync(db)` — main async function. Beat task
  `kg_runtime_correlation_sync` every 30 minutes.
- **Method.** Sliding window `RUNTIME_CORRELATION_LOOKBACK_DAYS` (default 7d)
  over `kg_pod_events`. For every pair (src, dst) where an edge already exists,
  co-occurrences are counted within `RUNTIME_CORRELATION_WINDOW_MINUTES` (default
  15 min). If count ≥ `RUNTIME_CORRELATION_MIN_COUNT` (default 2), the edge is
  confirmed: `populator.upsert_edge` with `discovered_by = "kg_sync/runtime_corr"`.
  `populator` adds the source to `extras.discovery_sources` and refreshes
  `last_seen_at`.
- **Confidence integration.** `kg_sync/runtime_corr` is registered in
  `confidence._SOURCE_PRECEDENCE` as a tier-1 source (precedence 0.95).
  Multi-source provenance (env + runtime_corr) → the highest confidence badge in the
  Discord embed.
- **Does not create new edges.** By design — a symmetric co-fail signal does not
  determine direction. If there is no edge (src, dst, kind) in the KG, runtime
  correlation will not add it. Direction discovery stays with declarative sources
  (env, Service selector, Ingress, NATS monorepo).
- **Reasons whitelist** (`DEFAULT_CORRELATION_REASONS`): BackOff, Unhealthy,
  OOMKilled, FailedScheduling, CrashLoopBackOff, FailedMount, ImagePullBackOff.
  A narrow diagnostic subset — NodeNotReady and similar cluster-wide reasons are
  excluded: they give a false positive for every pair of services on the node.
- **Synthetic exclusion.** `_is_synthetic()` filters synthetic nodes
  (NATS cluster, ingress:host, subject:* etc.) — their pod events come through the
  cluster-wide kubelet.
- **Feature flag:** `RUNTIME_CORRELATION_ENABLED=true` (on by default). With
  `False` the task is skipped with an info log.
- CLI: `python -m app.knowledge_graph.runtime_correlation`.

### Wave 7-Z: NATS subjects parser (`app/knowledge_graph/nats_subjects_sync.py`)
- `sync_nats_subjects(db, monorepo_path=...)` — main entry point. Beat task
  `kg_nats_subjects_sync` every 6 hours @ minute=43 (offset from drift/ingress/stuck).
- **Stage 1 — git sync** (`_ensure_monorepo`): shallow clone (`--depth=1`) +
  sparse-checkout (`GR.Platform`, `GR.Platform.Features`, `GR.WO.*`) into
  `WO_MONOREPO_PATH` (default `/var/lib/sre-ai/wo-monorepo`). On later runs —
  `git fetch origin master + reset --hard`.
- **Stage 2 — parse** (`parse_monorepo` → `parse_csharp_text`):
  - **Subject constants resolver** (`_load_subject_constants`) — one pass over
    `GR.Platform/DataBus/Nats/NatsConst.cs`, collects `NatsSubjectConst.<NAME>` →
    literal string.
  - **Subscribers** — regex on classes derived from `NatsJetStreamConsumer<T>` /
    `NatsJetStreamBatchConsumer<T>` / `MapNatsJetStreamConsumer<T>` /
    `MapNatsJetStreamBatchConsumer<T>`. Subject from
    `Subject => NatsSubjectConst.<NAME>` or `Subject => "literal"`.
  - **Publishers** — regex on `SendToJetStreamAsync(...)` / `PublishAsync(...)`;
    the first argument or named `subject:` = `NatsSubjectConst.<NAME>` or a literal.
  - **Service name** (`_service_name_from_path`): path `GR.WO.<X.Y.Z>/...` →
    `<x>-<y>-<z>` (lowercase, dots to dashes). Examples: `GR.WO.Map.Service/...` →
    `map-service`, `GR.WO.MapCoordinator.Service/...` → `mapcoordinator-service`,
    `GR.WO.City.Workers/...` → `city-workers`. These names match Deployment names in
    k8s.
- **Stage 3 — persist** (`persist_to_kg`):
  - Subject = synthetic Service in the `nats-subjects` namespace,
    `name = subject:<value>`. Hidden by `synthetic=True`.
  - Edge `uses_nats`, src=service, dst=subject. `extras.direction ∈ {pub, sub}`,
    `weight = count(call-sites)`. Idempotent by `(src_id, dst_id, kind,
    extras.direction)`.
- **Failure mode.** Git/ssh failure → logger.warning + skip; tests use
  `tests/kg/fixtures/nats_csharp/` without network calls.
- **Feature flag:** `NATS_SUBJECTS_PARSER_ENABLED=false` (off by default — needs ssh
  access to the GitLab monorepo). Enable deliberately after a manual `--dry-run`.
- **Env vars**: `WO_MONOREPO_PATH`, `WO_MONOREPO_SSH_URL`, `WO_MONOREPO_SPARSE_DIRS`.
- CLI: `python -m app.knowledge_graph.nats_subjects_sync [--dry-run] [--path PATH]`.

### Wave 8-A: k8s Jobs/CronJobs sync (`app/knowledge_graph/k8s_jobs_sync.py`)
- `sync_k8s_jobs(db)` — main entry point. Beat task `kg_jobs_sync` every 15
  minutes runs `kubectl get jobs,cronjobs -A -o json` and upserts into the table
  `kg_k8s_jobs` (separate from `kg_services` — a Job/CronJob is not a "service" and
  has no permanent pod).
- **Job fields** (`kind='job'`): `succeeded_count` / `failed_count` /
  `active_count` / `completion_time` / `last_pod_exit_code`. The last exit code comes
  from the podStatus of the latest pod by label selector `job-name=<name>`.
- **CronJob fields** (`kind='cronjob'`): `schedule` / `last_schedule_time` /
  `last_successful_time` / `suspended`.
- **Semantic edge `runs_as_job`** — NOT a separate row in `kg_service_edges` but the
  `K8sJob.owner_service_id` metadata column (FK to `kg_services.id`). The owner is
  resolved via label `app.kubernetes.io/part-of` or `app` matching `kg_services.name`
  in the same namespace. No match — owner is simply NULL (no bloat).
- **Failure mode**: subprocess.TimeoutExpired or a non-zero kubectl exit —
  log.warning, the beat tick returns an empty result, no raise.
- CLI: `python -m app.knowledge_graph.k8s_jobs_sync [namespace]`.

### Wave 8-B: k8s storage sync (`app/knowledge_graph/k8s_storage_sync.py`)
- `sync_storage(db)` — main entry point. Beat task `kg_storage_sync` every
  30 minutes (storage rarely changes — a claim about once a week, capacity is static —
  but Bound→Released phase transitions matter within half an hour).
- **Separate passes**:
  - PV (cluster-scoped, `namespace=''`).
  - PVC (namespace-scoped).
  - Scan of `pod.spec.volumes[]` cluster-wide for `uses_volume` edges.
- **Edges go to `kg_volume_edges`** (heterogeneous, tagged `src_kind`/`dst_kind`,
  no FK constraint):
  - `uses_volume` (Service → PVC): for every
    `pod.spec.volumes[].persistentVolumeClaim.claimName` → edge from the owning
    Service (via ownerReference Deployment/StatefulSet/RS).
  - `bound_to` (PVC → PV): via `pvc.spec.volumeName`.
- **disk_pct enrichment** behind `STORAGE_METRICS_ENABLED=false` (default OFF).
  PromQL: `100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes`
  per `(namespace, persistentvolumeclaim)`. If the `kubelet_volume_stats_*` scrape
  config is missing, every answer is 0 and cannot be told apart from "really unused".
- CLI: `python -m app.knowledge_graph.k8s_storage_sync`.

### Wave 8-D: stale classifier (`app/knowledge_graph/stale_classifier.py`)
- `classify_stale_with_deploys(name, ns, last_deploy_at, team_owner)` →
  `'active'` / `'expected_stale'` / `'suspicious_stale'`. Canonical values are
  re-exported from `contract.STALE_CLASS_VALUES`.
- **`expected_stale` heuristics**:
  - Suffix `*-backup` / `*-cron` / `*-cronjob` / `*-job`.
  - Infix `backup-` / `-backup-` / `-cron-`.
  - System namespaces: `kube-system`, `cattle-system`, `monitoring`, etc.
  - `team_owner ∈ {infra, platform}` outside `ACTIVE_WINDOW_DAYS`.
- `ACTIVE_WINDOW_DAYS` default 30, overridable via env.
- **Usage**:
  - `kg_sync.sync_namespace` — rewrites `kg_services.stale_class` idempotently on
    every sync.
  - `stats_digest.stale_deployments_section` — reads the column first, falls back to
    the legacy `_classify_stale` when the column is empty (an old install without a
    fresh sync).
  - SQL: `WHERE stale_class = 'suspicious_stale'` for dashboards.
- The value `gone` (contract 2.8, namespace removed from the cluster) is set by
  `namespace_lifecycle`, not by this classifier.

### KG schema/quality contract (`app/knowledge_graph/contract.py`)
- **The single source of truth** for what the KG counts as service / orphan /
  synthetic / owner-known, and which edge kinds are allowed.
- `KG_SCHEMA_VERSION: str = "2.9"` — current contract version. Bump rules in
  `docs/KG_SCHEMA_CONTRACT.md` §8 (major = breaking, minor = additive).
- `EDGE_KINDS: Dict[str, EdgeKindSpec]` — registry of all edge kinds. Each spec has
  `semantic`, `src_kinds`, `dst_kinds`, `source`, `status`
  (`active` / `planned` / `deprecated`), `table` (`kg_service_edges` /
  `kg_volume_edges` / `fk_only` / `metadata_only`).
- `REAL_SERVICE_KINDS` / `SYNTHETIC_KINDS` / `OWNER_SOURCES` /
  `OWNER_SOURCE_ALIASES` / `STALE_CLASS_VALUES` — canonical sets.
- **Utilities**: `is_synthetic(svc)`, `is_orphan(svc, edges)`,
  `service_kind_of(svc)`, `owner_known(svc)`,
  `STARTUP_CONTRACT_CHECK(db)` (boot-time drift logging).
- **Gate #22** — `tests/test_contract_drift.py` (auto-validation that the real kinds
  in the DB match the registry; blocks CI).
- Human-readable version — `docs/KG_SCHEMA_CONTRACT.md`.

### Multi-signal owner inference (`app/services/ownership_suggester.py`)
- Эвристика предложения owner-а для unowned-namespace. Используется в
  `stats_digest` секции `Unowned namespaces`.
- **Архитектура multi-signal** (взвешенный fusion):
  - **A. Prefix** (weight 0.4) — regex-таблица по ns-имени:
    `squad-N-*` → `@squad-N`, `<env>-kingdom<N>` → `@kingdom-N`,
    bare `monitoring`/`kube-system` → `@platform`, и т.п.
  - **B. Deploy history** (weight 0.4) — most-frequent `triggered_by`
    из `kg_deployments` за последние 30 дней. Username транслируется
    через `owner_aliases.resolve_username` → `@squad-N`. Покрывает
    кейс «ns без префикса, но один человек туда стабильно деплоит».
  - **C. Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
    `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
  - **Manual override** — `OWNERSHIP_MANIFEST_PATH=ownership.yaml` со
    списком `[{ns_pattern, owner, reason}]`. Match по pattern (glob) →
    confidence=1.0, эвристики игнорируются.
- API: `suggest_owner_multi_signal(db, ns) → OwnerSuggestion(owner,
  confidence, sources, reasoning)`. Top-1 победитель по сумме
  `weight × signal_strength`. Confidence clamp `[0, 1]`.
- **Backward compat**: старая `suggest_owner_for_ns(ns)` оставлена как
  deprecated wrapper.

### Owner aliases (`app/services/owner_aliases.py`)
- TC username → team mapping для owner inference (сигнал B).
- **Источники маппинга** в порядке приоритета:
  1. YAML-файл из ENV `OWNER_ALIASES_PATH` (deployment-specific override).
  2. Pre-baked `_DEFAULT_ALIASES` в коде (`lima → @squad-1` и т.п.,
     подтверждено по recent_deploys digest-у).
  3. Fallback `@?-{username}` — caller возвращает для неизвестных.
- API: `resolve_username(username: str) → str` (lowercase'ит вход).

### Quality report (`app/scripts/quality_report.py`)
- Идемпотентный read-only CLI: 5 групп метрик из Postgres KG-БД (`kg_services`,
  `kg_service_edges`, `kg_volume_edges`, `kg_alerts`, `kg_deployments`,
  `kg_k8s_jobs`, `kg_storage_volumes`, `kg_pod_events`).
- **Use case**: точка отсчёта для Phase A (remediation), чтобы demonstrably
  улучшать metrics, а не угадывать. После 17 PR Wave 7 + Wave 8 нужен
  baseline-снимок перед Phase A.
- **Без записи в БД** — никаких INSERT/UPDATE/DELETE. Использует ту же
  `SessionLocal` что и production-копилот; для unit-тестов
  `build_report(db)` принимает session-объект напрямую.
- **CLI**:
  ```bash
  python -m app.scripts.quality_report                    # markdown в stdout
  python -m app.scripts.quality_report --json             # JSON в stdout
  python -m app.scripts.quality_report --markdown --output baseline.md
  ```
- Baseline snapshot: `docs/quality_report_baseline_2026_05_24.md`.

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


## Evidence & Incidents (v1.0.4 – v1.0.8)

### Source status (`app/knowledge_graph/source_status.py`)
`SourceStatus` ∈ `success / partial / empty / unavailable / failed / invalid`; `mark()` / `status_of()` / `status_from_counts()`. `_record_beat_heartbeat` writes the heartbeat only for `success` / `partial`, the status always (`stats:beat:last_run:status:<task>`); `self_health.check_sync_lag` shows it as `last_status` and reports `disabled` for sources switched off by settings.

### Deploy watch (`app/knowledge_graph/k8s_deploy_watch.py`)
`watch_k8s_rollouts`: Redis snapshot `kg:workload_revisions` (uid, `template_hash`, images); a rollout = images or `spec.template` hash changed (never `metadata.generation`); dedup key `uid:template_hash`; rows land on the service node with `attribution=k8s_rollout`, `namespace_scope=false`, `rollout_reason` ∈ `image` / `template`.

### Incidents (`app/knowledge_graph/incidents.py`)
`attach_alert` (open → reopen within `REOPEN_WINDOW_MIN=30` → new; idempotent by fingerprint; sets `kg_alerts.incident_id`), `reconcile_incidents` (`all_alerts_resolved` / `aged_out` after `AGE_OUT_HOURS=168`), `mark_incident_noise` (per-alert kinds in `extras.noise_fingerprints`, `noise = all alerts noisy`), `incident_to_dict`. Model `KGIncident` (`kg_incidents`) with the partial unique index `uq_kg_incidents_one_open_per_service`.

### Incident timeline (`app/knowledge_graph/incident_timeline.py`)
`build_timeline(db, incident)` → `{incident, window, events[], counts, unknowns[], memory}`; event = `{ts, kind, title, details, evidence: {epistemic, provenance}}`; kinds: `incident.opened`, `deploy`, `pod_event`, `anomaly`, `log_errors`, `alert.fired`, `evidence`, `diagnosis`, `decision`, `action.applied`, `verification`, `alert.resolved`, `incident.resolved`. `LOOKBACK_MIN=60`, `LOOKAHEAD_MIN=30`.

### Blast radius (`app/knowledge_graph/blast_radius.py`)
`blast_radius_v2(db, ns, service, max_hops=2, limit=200, include_inactive=False)`: superset of `queries.blast_radius_for` + `target`, `impact[]` (`service, via, hops, path, epistemic, badge, edge_epistemic, reasons, conflicts`), `summary`, `unknowns[]`. Uses `epistemic.classify_edge` / `find_edge_contradictions`.

### Job attribution (`app/knowledge_graph/job_attribution.py`)
`resolve_job_target(db, ns, job_name)` → `(target, how)` with `how` ∈ `job_owner / cronjob_owner / cronjob_name / job_name`; `cronjob_base_name` strips the `-<unix-minutes>` suffix (≥ 8 digits). Used by `resolve_store_service` (store path, dedup key, resolved notice) and `enrich_alert`.

### Health retention (`app/knowledge_graph/health_retention.py`)
`RETENTION_TARGETS` (six observation tables) purged in batches by `kg_health_retention`.

### Self-health metrics (`app/knowledge_graph/self_health_metrics.py`)
`publish_snapshot` / `read_snapshot` (Redis `stats:self_health:last`), `SelfHealthCollector` (prometheus_client Collector) registered at API startup; `k8s/monitoring.yaml` holds `VMPodScrape` + `VMRule sre-ai-copilot`.

### Remediation verification (`app/remediation/verification.py`)
`TargetSnapshot`, `snapshot_target(intent)` (read-only `kubectl get -o json` via `kubectl_breaker.run_kubectl`), `expected_identity(db, incident_id)` (last `kg_remediation_decisions.target_ref`), `identity_mismatch` / `identity_check`, `schedule_verification` (Celery `remediation_verify`, `REMEDIATION_VERIFY_DELAYS_SEC`), `assess()` (pure), `verify_remediation()` → `analysis["executor_verification"]`, `executor_state` `verified` / `verification_failed`, audit `EXECUTOR_VERIFY_<OUTCOME>`, metric `remediation_verification_total`.

## Remediation (`app/remediation/`)
- `playbook.py`: YAML loader + strict schema (`remediation.playbook/v1` — preview only, `/v2` — executable: `match.alertnames`, `preconditions`, `plan.steps`, `verify`); `load_registry()` skips files starting with `_`. Registry: `app/remediation/registry/` (`restart_crashloop_deployment`, `scale_out_cpu_throttled_deployment` — v2; `cleanup_stale_failed_job` — v1; `_executor_apply_gate.yaml` — gate policy). Contract: `docs/REMEDIATION_PLAYBOOK_CONTRACT.md`.
- `matcher.py`: `match_playbooks` (v2 only; `match` + `preconditions` over a `FactStore`, no LLM), `render_plan` (a template without a value → `PlanRenderError`), `classify_alert`.
- `binding.py`: server-side match snapshot (`build_match_snapshot`) and intent binding (`bind_server_params`; server parameter `current_replicas`).
- `executor_gate.py`: `evaluate_intent_gate` — deterministic server risk gate on the apply path; policy from `_executor_apply_gate.yaml` combined with the playbook policy by the strictest verdict.
- `policy.py`, `risk_axes.py`, `classifier.py`, `target_resolver.py`, `preview.py`: Phase A layer — risk axes, policy evaluator (`auto` / `approve` / `block`), rule-based classification, target resolution, decision preview.
- `bridge.py`: deterministic bridge rules → v2 playbook → card with buttons (`REMEDIATION_BRIDGE_ENABLED`). `wants_bridge` (flag + `alertname` of an executable playbook), `collect_facts` (rule facts + K8sFacts pod state), `plan_proposals`, proposal dedup per (playbook, uid, incarnation) for `REMEDIATION_BRIDGE_DEDUP_SECONDS` (Redis `SET NX`, fallback `kg_remediation_decisions`).
- `bridge_dispatch.py`: enqueueing from the API process (since 1.0.32): `namespace_allowed` (`REMEDIATION_BRIDGE_NAMESPACES`), enqueue dedup (Redis `SET NX EX`, `REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS`), `apply_async` of the Celery task `remediation_bridge`. Redis down → nothing is enqueued (fail-closed).
- `attempts.py`: execution attempts (`kg_remediation_attempts`): claim, repeat blocking, last applied attempt.
- `models.py`: ORM `kg_remediation_decisions`.
- `verify_checks.py`: named outcome checks for the playbook `verify` section (`same_identity`, `action_took_effect`, `converged`, `healthy`, `no_new_crash_events`).

## Data & Persistence
- `app/database.py`: Engine/session helpers and database integration; schema migrations in `alembic/`.
- `app/models/`: domain models — `__init__.py` (SQLAlchemy `Base`, `Conversation`, `Message`), `incident.py` (Pydantic `AlertManagerAlert`, `AlertManagerWebhook`, `Incident`, `FullAnalysisReport`), `remediation_event.py` (`RemediationEventIn`).
- `app/repository.py`: CRUD operations for conversations and messages.

## Services & Safety
- `app/services/mcp_client.py`: Client for executing tools on external MCP servers.
- `app/services/teamcity_service.py`: TeamCity integration for deploy analysis via MCP.
- `app/services/approval_manager.py`: Redis-based approval lifecycle.
- `app/services/k8s_guard.py`: Policy check (verb/resource/namespace/body); namespace lists come from `app/security/namespaces.py` (forbidden / read-only / write patterns).
- `app/services/executor_apply.py`: `apply_intent` — the single code path for executing an approved `ExecutionIntent` (claim, re-dry-run, write, verification scheduling).
- `app/services/alert_target.py`: `resolve_alert_target` — attributes an alert to the k8s object (workload, job, PVC, pod, node, namespace) rather than to the metric exporter; the outcome goes to `kg_alerts.raw.target_kind`.
- `app/services/alert_dedup.py`: Discord card dedup (chronic suppress, rollout-noise silent, `SUPPRESS_REPEAT` for a repeat of a still-firing alert).
- `app/services/chronic_digest.py`, `app/services/stats_digest.py`, `app/services/weekly_reconciliation.py`: chronic alerts digest (every 6 h), daily stats digest, weekly reconciliation against the cluster (Mon 06:00 UTC, #stats).
- `app/core/execution_dsl.py`: Strongly typed `ExecutionIntent` and kubectl translator.
- `app/services/resilience.py`: Retry/circuit-breaker logic around LLM calls.
- `app/services/discord/` (`service.py`, `embed_builder.py`, `dedup.py`, `dedup_store.py`, `routing.py`; `app/services/discord_service.py` is a backward-compat shim): Discord notification dispatch. Renders incident embeds, manages 30-min dedup (PATCH via `webhook?wait=true`), severity routing, per-team channel routing (`DISCORD_TEAM_CHANNEL_MAP`), suspect-deploy / log-error / anomaly / recurrence / linked-alerts blocks, and Approve/Decline action rows.
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
- `app/telemetry.py`: tracing (OpenTelemetry).
- `app/observability/logging_setup.py`: `configure_logging()` — structlog level from `LOG_LEVEL` (default INFO), WARNING for httpx/httpcore (request URLs carry tokens). Called in `app/main.py` and `app/workers/tasks.py`; the console format is unchanged (1.0.33, replaced the unused `app/logger.py`).
- `app/observability/access_log.py`: uvicorn access-log filter that drops successful kubelet probes (`/healthz`, `/readyz`); installed from `app/main.py`.
- `app/observability/ai_metrics.py`, `app/observability/worker_metrics.py`: AI pipeline metrics and Prometheus export from Celery workers.
- `app/metrics.py`: Prometheus application metrics.
