# SRE AI Copilot — Architecture

State as of v1.0.33 (2026-11-01).

## 1. System Overview

The application consists of: an HTTP API (FastAPI), Celery workers and a beat scheduler, PostgreSQL (the Knowledge Graph and incident records), Redis (Celery broker, dedup keys, beat heartbeats, self-health snapshot), Discord as the output channel, and Kubernetes as the observed system (and, through a separate executor, the narrowly acted-upon one).

In production the alert path is **deterministic**: alerts are written to the KG, enriched from it with SQL and rules, and rendered as Discord cards. The fact-anchored multi-hypothesis LLM pipeline (§3, §4) is still in the code, but **no Alertmanager route points to it and it is switched off by `LLM_PIPELINE_ENABLED=false`** — `process_incident` returns `skipped` without calling a model.

### 1.1 Production alert path

```
VMAlertmanager (k8s/vmalertmanagerconfig.yaml)
 │
 ├─ prod-*, preprod-*, and the root fallback (alerts without namespace:
 │  nodes, control plane, monitoring/kube-system)
 │    → POST /webhooks/alertmanager/enrich-and-forward      (max_alerts 50)
 │        → KG write (kg_alerts, kg_incidents, populate_from_incident)
 │        → deterministic enrichment (alert_enrichment, no LLM)   §3a
 │        → decide_send (chronic / repeat / rollout suppression)
 │        → Discord card in #infra-error
 │        → wants_bridge? → enqueue `remediation_bridge` (Celery)  §2.3
 │
 ├─ squad-*, preupdate-*, any severity=info
 │    → POST /webhooks/alertmanager/store                   (max_alerts 0)
 │        → KG only — no Discord card (deliberate, not a gap)
 │        → wants_bridge? → enqueue `remediation_bridge`
 │
 ├─ alertname=Watchdog, severity=none → blackhole
 │
 └─ (no route) POST /webhooks/alertmanager → full LLM pipeline   §3
                (LLM_PIPELINE_ENABLED=false)

External: squad-medic → POST /webhooks/remediation → kg_remediation_events  §2.4
```

The alert target (which k8s object the alert is about) is resolved by one label-priority chain in `app/services/alert_target.py`: an alert about an exporter itself lands on the exporter; otherwise `deployment`/`statefulset`/`daemonset` → `cronjob`/`job_name` → HPA/PDB → app alert → node → PVC → pod → container → `scrape_job`/`job=<ns>/<name>` → `service` → namespace. The outcome is stored in `kg_alerts.raw.target_kind` (`service` / `node` / `namespace` / `exporter` / `unresolved`).

Card rules worth knowing: a repeat of a still-firing alert sent by Alertmanager on `repeat_interval` is suppressed (`SUPPRESS_REPEAT`, metric `alerts_suppressed_total{reason="repeat"}`, flag `ALERT_SUPPRESS_AM_REPEATS`), not posted as «RESURFACED»; a green «✅ resolved» card is posted only for critical episodes of at least `DISCORD_RESOLVED_NOTICE_MIN_MINUTES` (30). Alerts that fire continuously for more than `STUCK_ALERTS_MIN_DURATION_HOURS` (24) appear in the chronic digest instead.

## 2. Runtime Components

### 2.1 Processes (namespace `sre-ai`, manifests in `k8s/`)

| Component | Kind | Role |
|---|---|---|
| `sre-ai-api` | Deployment, 2 replicas | FastAPI on :8000 (webhooks, `/discord/interactions`, `/kg/*`, `/copilot`, `/healthz`, `/readyz`); Prometheus metrics on :8001 |
| `copilot-worker` | Deployment, 2 × `--concurrency=2`, `-Q celery` | incident pipeline, `remediation_bridge`, `remediation_verify`, light beat tasks |
| `copilot-worker-sync` | Deployment, 1 × `--concurrency=2`, `-Q sync` | heavy KG syncs (since 1.0.28) |
| `copilot-executor` | Deployment, 1 × `--concurrency=1`, `-Q executor` | the only process with cluster write access: ServiceAccount `sre-ai-executor`; runs `executor_apply` / `executor_dry_run` |
| `copilot-beat` | Deployment, always 1 replica | Celery beat |
| `postgres` | StatefulSet (`postgres:15-alpine`, local-path PVC) | KG + incidents (`sre_copilot` DB) |
| `redis` | StatefulSet (local-path PVC) | broker, dedup, heartbeats, snapshots |
| `postgres-backup` | CronJob, 02:00 UTC | §2.6 |
| `squad-dashboard`, `ci-deadman` | CronJobs | squad dashboard; CI runner watchdog |

`postgres` and `redis` carry PriorityClass `sre-ai-copilot-stateful` (`k8s/priorityclass.yaml`, value 100000, `preemptionPolicy: Never`): their volumes are local-path and cannot move to another node, so they are evicted last.

Deploy: the image is built by CI and rolled out with `kubectl set image` on the five Deployments (plus `BUILD_VERSION` env, which the card footer reads); `deploy.sh` applies `k8s/` only when Deployment/NetworkPolicy/RBAC/PriorityClass manifests change. `deploy.sh` does not apply the `postgres`/`redis` StatefulSets — those are changed with `kubectl patch`. The Helm chart was removed in 1.0.28. Details — `docs/RUNBOOK.md`.

### 2.2 Celery queues and the beat schedule

| Queue | Consumer | What goes there |
|---|---|---|
| `celery` (default, `task_default_queue`) | `copilot-worker` | `process_incident`, `generate_reply`, `remediation_bridge`, `remediation_verify`, light beat tasks |
| `sync` (`SYNC_QUEUE_NAME`) | `copilot-worker-sync` | `SYNC_QUEUE_TASKS` routed by `task_routes`: `kg_topology_sync`, `tc_deploys_to_kg`, `kg_storage_sync`, `kg_metrics_sync`, `kg_endpoints_sync`, `kg_signal_aggregates_compute`, `kg_health_retention`, `kg_topology_resources_sync`, `kg_anomaly_detection_task`, `kg_runtime_correlation_sync`, `kg_nats_subjects_sync` |
| `executor` (`EXECUTOR_QUEUE_NAME`) | `copilot-executor` | `executor_apply`, `executor_dry_run` (when `EXECUTOR_DISPATCH=queue`, as in the manifests) |

Every beat task holds a `single_instance` lease, so a slow run is not overlapped by the next tick. Queue lengths are exported as `celery_queue_length{queue}` (see `docs/METRICS.md` §7).

Beat schedule (`app/workers/tasks.py`, `beat_schedule`, 36 entries), grouped:

| Group | Tasks (schedule) |
|---|---|
| Topology and inventory | `kg_topology_sync` (hourly :00), `kg_topology_resources_sync` (15 min), `kg_ingress_sync` (hourly :37), `kg_endpoints_sync` (15 min), `kg_jobs_sync` (15 min), `kg_storage_sync` (30 min), `kg_nodes_sync` (30 min), `kg_namespace_lifecycle` (10 min), `kg_namespace_owner_sync` (hourly), `kg_db_edge_rehome` (hourly), `kg_drift_cleanup` (hourly :17), `kg_ownership_backfill` (6 h), `kg_nats_subjects_sync` (6 h, off by default) |
| Events and deploys | `tc_deploys_to_kg` (15 min), `kg_deploy_watch` (5 min), `k8s_pod_events_sync` (10 min), `kg_alerts_resolve_sync` (15 min), `kg_incidents_lifecycle` (5 min), `kg_statics_versions_sync` (5 min) |
| Metrics and signals | `kg_metrics_sync` (10 min), `kg_cluster_health_sync` (5 min), `kg_ingress_observations_sync` (10 min), `kg_seq_logs_sync` (10 min), `kg_anomaly_detection_task` (10 min), `kg_signal_aggregates_compute` (hourly :23), `kg_runtime_correlation_sync` (30 min), `kg_health_recompute` (20 min), `kg_external_probe` (every minute) |
| Digests and reports | `daily_stats_digest` (daily, `STATS_DIGEST_HOUR_UTC`), `team_daily_digest` (daily, `TEAM_DIGEST_HOUR_UTC`), `chronic_alerts_digest` (6 h), `weekly_reconciliation` (Mon 06:00 UTC) |
| Supervision and housekeeping | `kg_self_health_check` (30 min), `kg_stuck_alerts_check` (hourly :11), `kg_health_retention` (daily 03:40 UTC), `discord_dedup_purge` (30 min) |

Not on beat: `remediation_verify` (`apply_async(countdown)` at +5 and +15 min after an apply, §3f), `remediation_bridge` (enqueued by the API), `process_incident` / `generate_reply`, executor tasks.

### 2.3 Deterministic remediation bridge

`app/remediation/` (bridge since 1.0.31, executed in Celery since 1.0.32). It lets the production alert path reach an action without the LLM pipeline.

```
API (/enrich-and-forward or /store)
  → wants_bridge(labels): flag + alertname present in the playbook registry
  → namespace_allowed: REMEDIATION_BRIDGE_NAMESPACES (prefixes; empty = all)
  → enqueue dedup: Redis SET NX EX on (alertname, namespace, pod/workload),
    REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS (600) + dedup inside the batch;
    Redis unavailable → not enqueued (fail-closed)
  → apply_async `remediation_bridge` (queue celery, soft/hard limit 120/180 s)

Worker: remediation_bridge
  → rule facts from enrich_alert + pod state from K8sFacts
  → match_playbooks (app/remediation/registry/*.yaml)
  → intent built by the server from the playbook's plan.steps
  → match snapshot + bind_server_params → gate (policy)
     approve → target snapshot (uid), server dry-run,
               IncidentRecord(APPROVAL_PENDING) + kg_remediation_decisions,
               card with Apply / Decline buttons in #infra-error (bot)
     block   → no buttons; "playbook X: blocked by policy" is PATCHed into
               the already-sent alert card (prod / preprod / system)
  → proposal dedup: (playbook, target uid, incarnation) for 6 h
    (REMEDIATION_BRIDGE_DEDUP_SECONDS)
```

- Code: `bridge.py` (worker side), `bridge_dispatch.py` (cheap API-side enqueue), task `remediation_bridge` in `app/workers/tasks.py`. The 1.0.31 version ran the bridge inside the API event loop and took the API down (incident 2026-10-31); the API now only enqueues.
- The bridge only takes executable playbooks matched by alertname (`_candidate_alertnames()` in `bridge.py`). Two qualify: `restart_crashloop_deployment` (`KubePodCrashLooping` / `PodCrashLooping`; excludes OOM, a fresh deploy and signal exits — exit codes 132/134/135/136/138/139) and `scale_out_cpu_throttled_deployment` (`CPUThrottlingHigh`; approve only on dev/squad tiers; +1 replica from the live `spec.replicas`). `cleanup_stale_failed_job` matches by `classification` only, so the bridge does not take it.
- Buttons appear only with `EXECUTOR_ENABLED` and `EXECUTOR_APPROVAL_ENABLED`. Squad alerts still do not go to the channel; the only exception is a proposal card with policy `approve`.
- Flags: `REMEDIATION_BRIDGE_ENABLED`, `REMEDIATION_BRIDGE_NAMESPACES`, `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED` (all default off in `app/config.py`; in production they are set in Secret `sre-ai-secrets`, not in the `k8s/` manifests, and are on). New flags are enabled on one namespace under load first.

Button click path:

```
Discord → the bot application's Interactions Endpoint (ai-reviewer, a separate
          service in ns ai-reviewer-net)
  → relays approve:/decline: → POST /discord/interactions (sre-ai-api :8000)
     (NetworkPolicy admits ai-reviewer-net → API)
  → Ed25519 signature check with DISCORD_PUBLIC_KEY (in the copilot)
  → allowlist DISCORD_APPROVERS_USER_IDS (fail-closed; role allowlist exists
    in code but is not configured in production)
  → executor_apply (queue executor, copilot-executor, SA sre-ai-executor)
  → remediation_verify at +5 / +15 min → result appended to the same card
```

The relay returns the copilot's response body to Discord unchanged.

### 2.4 External remediation events (squad-medic)

`POST /webhooks/remediation` accepts run reports from an external executor — the squad-medic CronJob, which treats squad stands every 15 minutes. Auth is HMAC-SHA256 over `timestamp.body` with a mandatory timestamp and anti-replay (`REMEDIATION_WEBHOOK_SECRET`, fail-closed). A repeat of `(actor, run_id, namespace)` is idempotent. Events land in `kg_remediation_events`, are linked to the incident open at that moment on any namespace of the stand, and appear in the incident timeline as `remediation.external`. The `fixed` field means «the stand was healthy after treatment».

### 2.5 Supervision

- **Self-health** (`kg_self_health_check`, every 30 min): 15 checks over KG data and sources (§3c), snapshot in Redis `stats:self_health:last`, exported as metrics by the API and alerted on by `VMRule sre-ai-copilot` (`docs/METRICS.md` §7).
- **Weekly reconciliation** (`weekly_reconciliation`, `app/services/weekly_reconciliation.py`, Mon 06:00 UTC, #stats, `WEEKLY_RECONCILIATION_ENABLED`, default on): compares the copilot against the outside world — alert coverage (`ALERTS` in VictoriaMetrics vs `kg_alerts` for 7 days), table growth (top-5 by size vs last week), backup age (`kube_cronjob_status_last_successful_time`), share of alerts without attribution, confirmed fixes (verified executor attempts + medic `fixed`). An unavailable source reports «not measured», not zero.
- **Digests**: `daily_stats_digest`, `team_daily_digest`, `chronic_alerts_digest` (every 6 h, `CHRONIC_DIGEST_ENABLED`; includes alerts firing continuously longer than 24 h).

### 2.6 Data lifecycle

- **Retention** (`app/knowledge_graph/health_retention.py`, task `kg_health_retention`, daily 03:40 UTC, batched deletes). Samples (`RETENTION_TARGETS`, floor `MIN_RETENTION_DAYS`=7): `kg_service_health` (`KG_HEALTH_RETENTION_DAYS`, 30), `kg_anomaly_observations`, `kg_signal_aggregates`, `kg_ingress_observations`, `kg_log_observations` — 30 days; `kg_cluster_observations` — 90. Events (`EVENT_RETENTION_TARGETS`, floor `MIN_EVENT_RETENTION_DAYS`=90): `kg_deployments` 180 days by `started_at`, `kg_alerts` 180 and `kg_incidents` 365 by `resolved_at` (open rows are never deleted), `kg_pod_events` 90 by `last_seen`, `kg_remediation_events` 365.
- **Write volume**: KG upserts skip unchanged rows (`SERVICE_TOUCH_INTERVAL` 30 min, `EDGE_TOUCH_INTERVAL` 3 h in `populator.py`); `k8s_endpoints_sync` writes metadata only when `endpoints_ready` changes.
- **Backup** (`k8s/postgres-backup.yaml`, CronJob 02:00 UTC): `pg_dump` (custom format, gzip) onto PVC `postgres-backup-data` on a different node than the database, keeping the last `BACKUP_KEEP` (7) dumps, then `aws s3 cp` to the bucket from Secret `postgres-backup-s3` (upload is skipped if the secret is absent). Alerts: `CopilotPostgresBackupFailed` / `CopilotPostgresBackupMissing`.

### 2.7 Logging

`app/observability/logging_setup.py` `configure_logging()` is called in `app/main.py` (API) and `app/workers/tasks.py` (every Celery process: worker, worker-sync, executor, beat). Level from `LOG_LEVEL` (default INFO); `httpx`/`httpcore` are raised to WARNING because they log full URLs, including tokens in webhook paths. Output is the default structlog console renderer, not JSON. Celery does not hijack stdout (`worker_redirect_stdouts=False`); uvicorn access-log lines for `/healthz` and `/readyz` with status < 500 are dropped by `app/observability/access_log.py`. The audit log (`app/services/audit_logger.py`) writes JSON lines to stdout.

### 2.8 Modules

- **API app (`app.main`)**: routing, auth dependency, metrics middleware, health/readiness endpoints.
- **Webhooks (`app.api.webhooks`)**: the four endpoints of §1.1; `/webhooks/alertmanager` dispatches `process_incident`, which is gated by `LLM_PIPELINE_ENABLED`.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: background conversational analysis with a confidence-threshold loop (`generate_reply`, same Celery app).
- **DiagnosticsEngine (`app.diagnostics.engine`)**: rule-based evaluation producing a typed `FactStore`. Used by both the LLM pipeline and the deterministic path.
- **FactStore (`app.diagnostics.facts`)**: canonical ground-truth about the incident; supports conflict detection and prompt serialization.
- **MultiHypothesisAgent (`app.agents.multi_hypothesis`)**: fan-out to 4 perspectives (app/infra/deps/runtime) with precondition filtering. *LLM pipeline only.*
- **FactCriticAgent (`app.agents.fact_critic`)**: adversarial grounding — eliminates hypotheses that contradict observed facts. *LLM pipeline only.*
- **FixAgent (`app.agents.fix`)**: generates a structured `ExecutionIntent`; recurrence-aware and Jira-enriched. *LLM pipeline only.*
- **SimilarIncidentEngine (`app.core.intelligence.similar_incidents`)**: KG-based recurrence detection (7-day window).
- **JiraClient (`app.context.jira_client`)**: Atlassian REST API enrichment.
- **Knowledge Graph (`app.knowledge_graph.*`)**: auto-populating directed graph in Postgres. Core tables: `kg_services`, `kg_service_edges`, `kg_deployments`, `kg_alerts`, `kg_incidents`, `kg_pod_events`, `kg_namespaces`; storage `kg_storage_volumes` + `kg_volume_edges`; jobs `kg_k8s_jobs` + `kg_k8s_job_runs`; nodes and traffic entrypoints `kg_nodes` + `kg_entrypoints`; remediation `kg_remediation_decisions`, `kg_remediation_attempts`, `kg_remediation_events`; time series (§3b). Schema/quality contract — `app/knowledge_graph/contract.py` (`KG_SCHEMA_VERSION` = 2.9, history in `docs/KG_SCHEMA_CONTRACT.md`). A node carries `node_kind` (`service` / `workload` / `ingress`): a k8s Service and its backing Deployment are **different nodes**, so the `serves_traffic` edge between them can exist.
- **Alert enrichment (`app.services.alert_enrichment`)**: deterministic KG-based enrichment for `/webhooks/alertmanager/enrich-and-forward` — runs without LLM, builds `EnrichedContext` (recent_deploys, upstream_alerts, outgoing_deps, pod_events, jira_issues, primary_hypothesis, why_this_matters).
- **Remediation (`app.remediation`)**: playbook registry, matcher, binding, policy gate, attempts, verification, the deterministic bridge (§2.3).
- **Data layer (`app.database`, `app.repository`, `app.models`)**: SQLAlchemy models and CRUD operations.
- **Integration layer (`app.services.mcp_client`)**: MCP client for k8s, TeamCity, and other external tools.
- **Safety services**: approval manager, K8s guard, execution DSL, executor gate (`app/remediation/registry/_executor_apply_gate.yaml`).

## 3. Data Flow (LLM Incident Pipeline — not routed in production)

> **Status:** no Alertmanager receiver points to `/webhooks/alertmanager`, and `process_incident` returns `skipped` while `LLM_PIPELINE_ENABLED=false` (the default and the production value). Enabling it also requires a finite `LLM_DAILY_BUDGET_USD` (config validator). The description below is the code path, kept for reference.

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

## 3a. KG-only Enrichment Flow (without LLM)

The production path (§1.1): deterministic alert enrichment without LLM, the only Discord output channel for alerts. `/webhooks/alertmanager/store` shares the KG write (and the remediation-bridge enqueue) but sends no card.

```
AlertManager webhook
  → POST /webhooks/alertmanager/enrich-and-forward
  → store incident → KG (populate_from_incident)
  → group alerts by (alertname, severity)
  → for each group:
     → alert_enrichment.enrich_alert(db, incident)
        → recent_deploys_for(...)         # with adaptive effective_at
        → nearby_alerts(...)              # upstream alert correlation
        → incidents_on(...)               # recurrence window
        → _downstream_count_by_kind(...)  # inbound callers
        → upstream_of(..., fresh_only_days=30)  # outgoing deps with confidence
        → recent_pod_events_for(...)      # k8s diagnostic signal
        → JiraClient.search_by_service_sync(...)  # ticket linkback
        → PodEventsRule + RecentDeployRule + UpstreamDegradedRule
        → primary_hypothesis() + why_this_matters()
        → noise flags: rollout_noise / meta_noise / gen_mismatch_noise (set on EnrichedContext, see §3a-noise)
     → decide_send() (chronic suppress / rollout-silent)
     → DiscordService.send_enriched_alert(contexts, env, resurfaced)
        → severity-aware embed (muted render — grey + 🔇 — when a noise flag is set)
        → confidence badges (●●●/●●○/●○○) with provenance
        → clickable TC build links + deployer name
     → wants_bridge? → enqueue remediation_bridge (§2.3)
```

Latency budget: <500ms p95 synchronous in HTTP handler. No LLM tokens. Total cost: 0 per alert.

### Alert-quality noise suppression — three distinct mechanisms (v0.14.0)

The pipeline suppresses noise at **three** different layers. They are not interchangeable — keep the taxonomy straight:

| # | Mechanism | Where | Effect on the card | Severity | Examples |
|---|---|---|---|---|---|
| 1 | **DROP at ingest** | webhook handler, **before** KG-store and enrichment | card never appears at all | n/a (filtered out) | `ALERT_SUPPRESS_NAMES` (default `Watchdog`, `InfoInhibitor`, `KubeAPIServerSlo…`), extendable via `ALERT_SUPPRESS_NAMES_EXTRA` |
| 2 | **MUTE at render** | render / `send_enriched_alert` | visible but quiet — grey color + 🔇, no 🚨, no @mention; severity is **not** demoted, card is **not** dropped | preserved | `rollout_noise`, `meta_noise`, `gen_mismatch_noise` (below) |
| 3 | **chronic suppress** | `decide_send()` | folded into the periodic chronic digest instead of a live ping | preserved | repeatedly-firing chronic alerts (see `chronic_alerts_digest`) |

The three **MUTE** classes (mechanism #2) are set as boolean flags on `EnrichedContext` in `enrich_alert` (step 9 of the enrichment flow above) and consumed by the muted render path in `app/services/discord/service.py` (color / icon / mention):

- **`rollout_noise`** (`ROLLOUT_SUPPRESS_ENABLED`, window `ROLLOUT_SUPPRESS_WINDOW_MINUTES`=15) — suppresses alerts that fire inside an active deploy window (a `kg_deployments` row for the resolved service exists within the last N minutes). Detector `_detect_rollout_noise(incident, recent_deploys)`.
- **`meta_noise`** (`META_NOISE_ENABLED`, default true) — **always-noisy** meta-aggregates: the `*NewCriticalAlerts` family (Prod/Preprod/Squad/…) plus the derived control-plane scrape-gap alerts `etcdInsufficientMembers` / `ScrapePoolHasNoTargets` / `RecordingRulesNoData`. Safe to mute because every real critical already arrives as its own loud card with service/deploy/KG context — the aggregate counter only restates it. Detector `_detect_meta_noise`, render tag **🔇 META-AGGREGATE**. Precedent: ProdNewCriticalAlerts 2026-06-16 (PR #154).
- **`gen_mismatch_noise`** (`GEN_MISMATCH_NOISE_ENABLED`, default true) — **conditional** churn of `KubeDeploymentGenerationMismatch`. `metadata.generation != observedGeneration` flaps benignly when an external controller (Rancher / cattle-cluster-agent writing the `publicEndpoints` annotation) bumps `generation` and the deployment controller lags `observedGeneration` for a moment — the rollout has long since converged. But the same alertname also fires for a genuinely stuck rollout. The discriminator is **replica health** `ctx.replicas_ready_desired`: mute **only** when `ready==desired (≥1)`; for `ready<desired` / `?/N` / `None` / `0/0` the alert stays **fail-safe LOUD** (a real stuck rollout rings in any namespace, prod included). Health-gating beats namespace-scope. Detector `_detect_gen_mismatch_noise(incident, replicas_ready_desired)`, render tag **🔇 GENERATION-CHURN**. Precedent: prod-kingdom7 / town-service 2026-06-23 (PR #160). `rollout_noise` takes precedence — `gen_mismatch_noise` is only evaluated when `rollout_noise` is false.

Tests: `tests/test_meta_noise_suppression.py` (12), `tests/test_gen_mismatch_noise.py` (14).

## 3b. Active Observability Layer (Wave 1–5)

Beyond the discrete event tables (deployments / alerts / pod_events), the KG also materialises a continuous time-series view of every service, used by the digest, the anomaly detector, the deploy correlator, and the Discord pipeline.

### Time-series materialization (Wave 1)

| Table | Source | Granularity |
|---|---|---|
| `kg_service_health` | VictoriaMetrics PromQL via `metrics_sync.py` | 10-min, per service |
| `kg_cluster_observations` | k8s node API via `cluster_health_sync.py` | 5-min, per node |
| `kg_ingress_observations` | nginx-ingress PromQL via `ingress_observations_sync.py` | ~10-min, per host/path |
| `kg_signal_aggregates` | 24-h roll-up via `signal_aggregates.py` | hourly refresh (:23), per service |

Every PromQL fetch is wrapped in a Postgres `SAVEPOINT` per row insert; an `IntegrityError` on the unique key (e.g. duplicate `(service_id, ts)`) rolls back to the savepoint and continues. This makes the sync naturally idempotent on overlapping windows.

Wave 5 PromQL gotcha (documented for future authors): `mem_pct` must be computed as `avg(rate(container_memory_working_set_bytes) / on(pod) kube_pod_container_resource_limits)` — i.e. divide first, aggregate second. The aggregate-then-divide form ran for several days returning silent 0s because the many-to-one join collapsed before the division. The kg_self_health canary (see below) was added specifically to detect this class of silent failure.

Coverage update (2026-06-10): nginx-ingress metrics are now enabled on both WO controllers (`--enable-metrics=true` on the shared `ingress-nginx-controller`, 16 nodes, and `ingress-prod-controller`, 5 prod nodes). Per-host labels are kept — `metrics-per-host` defaults to true and must NOT be disabled: the copilot sync filters by host. Scraping goes through VMPodScrape `ingress-nginx-shared` + `ingress-prod` in ns `cattle-system` with `honorLabels: true` (mandatory — otherwise the `namespace` label gets overwritten into `exported_namespace`), picked up by the VMAgent in ns `monitoring` (`selectAllByDefault`). As a result `kg_ingress_observations` is populated with per-host/path p95/p99/rps/4xx/5xx; the first 50 minutes produced 156 rows across 23 hosts covering all contours (prod/squad/preprod/preupdate/infra), with 100% of rows linked to `kg_services`.

Remaining honest limitation: per-service `http_5xx_rate` and `p95_latency_ms` in `kg_service_health` are STILL always 0 — `/metrics` of the ASP.NET services (Kestrel) is closed by JWT middleware (401). Backend ticket WO-12483 tracks the fix (recommendation: a separate management port without auth); once it ships, a VMServiceScrape on the app namespaces will be added and these fields come alive. Until then `health_score` remains an infra proxy (alerts + pod_events + aggregates), and `log_error_rate` is a log-derived proxy, not HTTP 5xx. The pipeline degrades gracefully — anomaly detection on a flat-zero metric simply produces no observations.

### Anomaly detection (Wave 2 + Wave 6 update)

`app/knowledge_graph/anomaly_detection.py` scans the materialised time series and writes `kg_anomaly_observations`.

- **Robust-z statistic.** `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Uses median + MAD instead of mean + stddev, so a single spike in the baseline doesn't poison the threshold.
- **Seasonal baseline.** When ≥50 historical points are available, baseline is stratified by hour-of-day to absorb daily traffic patterns. `extras.method = robust_z_seasonal`. Below that threshold the detector falls back to a flat baseline (`robust_z_flat`).
- **Configurable thresholds** via `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) and `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`).
- **Volume guard.** No more than 3 observations per (service, metric) per hour, to avoid flooding when a metric is genuinely sustained-anomalous.
- **Baseline window:** rolling 7 days.

### Deploy ↔ Incident correlator (Wave 2 + Wave 6 update)

`app/rca/deploy_correlator.py` is invoked inline by `IncidentPipeline._enrich_deploy_correlation` for every incident that reaches enrichment. This is part of the LLM pipeline (`app/workers/pipeline.py`), so it does not run in production while `LLM_PIPELINE_ENABLED=false`.

- **Window:** `[incident − 2h, incident]`. Every deployment of the affected service inside the window is a candidate.
- **Confidence formula.** Multi-factor score, each component clamped to `[0, 1]`:
  - `N_spikes`: number of anomaly observations of the service after the deploy
  - `max_zscore`: peak robust-z observed in the window
  - `time_proximity`: how close the deploy is to the incident start
  - `deploy_status_factor`: FAILED deploys score higher than SUCCESS
  - `flat_baseline_penalty`: if baseline was all-zeros (i.e. metric was dead anyway), confidence is dampened
- **Verdict tiers:**
  - `likely` if confidence ≥ 0.7 — surfaced as a "Suspect deploy" block in the Discord embed
  - `suspect` if 0.4 ≤ conf < 0.7 — surfaced with weaker language
  - `weak` if 0.2 ≤ conf < 0.4 — kept in the analysis JSON, not in the embed
  - `unlikely` if conf < 0.2 — recorded for audit but not shown
- Available downstream via `analysis["deploy_correlator"]` for the Discord renderer and any future RCA consumers.

### Seq logs integration (Wave 2)

`app/knowledge_graph/seq_logs_sync.py` polls the Seq REST API every 10 minutes and writes `kg_log_observations`, one row per `(service, level)` per 10-min window.

- Match algorithm: Seq event `Application`/`Service` property → `kg_services.name`, falling back to namespace prefix.
- Used by the Discord pipeline to render a "log error rate" block when anomalies coincide.
- Known limitation: at the moment the pipeline writes everything as `Information` because the available realms expose only a single combined stream — per-realm Seq instances or access-log routing would be needed to populate `Error`/`Warning` independently. The schema and downstream consumers are level-aware; only the source data is degraded.

### Daily team digest (Wave 2)

`app/services/team_digest.py` (task `team_daily_digest`) runs daily at `TEAM_DIGEST_HOUR_UTC` and produces a per-team Discord embed with:

- Fragile top-5 services by `health_score` (the lowest scores)
- Deploy success-rate over the last 24h
- Open alerts breakdown (firing / chronic-suppressed / resolved)
- Top alertnames over the last 24h
- SLO burn rate where available

### Discord pipeline overhaul (Wave 3)

The Discord renderer (`app/services/discord_service.py`) was reshaped from "send and forget" into a stateful publisher:

- **Dedup window**: 30 min. Repeat of the same logical alert PATCHes the existing message via `webhook?wait=true` instead of re-posting.
- **Severity routing**: `critical` / `warning` → channel post; `info` / `none` → silent (audit only). Configurable per env.
- **Per-team channel routing** via `DISCORD_TEAM_CHANNEL_MAP` (JSON: `{team_owner: webhook_url}`); fallback to the default webhook.
- **Suspect deploy block** rendered when `deploy_correlator.verdict ∈ {likely, suspect}`.
- **Log error rate block** rendered when `kg_log_observations` shows an anomaly in the same window.
- **Anomaly block** rendered from `kg_anomaly_observations`.
- **Recurrence block** — "×N in 24h · M in 7d" — driven by KG history.
- **Linked alerts aggregation** — alerts whose upstream/downstream services are also firing are folded into one embed.
- **Persistent approval** — `ActionApproval` ORM table with UNIQUE `(incident_id, intent_signature)`; Approve / Decline buttons hit the Discord interactions endpoint and record the decision.

## 3c. Security & Operational Hardening (Wave 6)

### PII redaction

`app/services/pii_redaction.py` provides a regex-based redactor with patterns for:

- email addresses
- IPv4 and IPv6 literals
- JWT tokens (3-segment base64)
- `Bearer …` authorisation headers
- UUIDs
- long hex strings (likely fingerprints / keys)
- `key=value` style secrets where key matches `password|token|secret|api_key`

Applied at two layers:

1. **Write-time** in `seq_logs_sync.py` — log lines are redacted before being persisted into `kg_log_observations.message_sample`, with a 500-character truncation. This keeps the database itself clean.
2. **Defense-in-depth** at embed-render time in `discord_service.py` — every string heading to a Discord webhook is run through the redactor a second time, on the assumption that any future data source might bypass write-time scrubbing.

The redactor is idempotent: running it twice produces the same output.

### Approve / Decline authorization

The Discord interactions endpoint (`app/api/discord_interactions.py`) now gates Approve / Decline button presses:

- `DISCORD_APPROVERS_USER_IDS` — comma-separated allowlist of Discord user IDs (the only list configured in production)
- `DISCORD_APPROVERS_ROLE_IDS` — comma-separated allowlist of Discord role IDs (the interaction member must hold at least one); supported, not used in production
- `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` — per-user quota, default 5

Semantics:

- **Fail-closed** when both lists are empty — the button is denied and audit-logged as `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.
- Unauthorized presses get an ephemeral deny reply and audit log `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
- Rate-limit is in-memory. An authz failure does NOT consume quota — a denied user can't be used to exhaust the limit for a legitimate approver.

### KG self-health canary

`app/knowledge_graph/self_health.py` is a "monitoring of the monitoring" beat task, triggered after the Wave 5 mem_pct silent-failure to make sure that class of regression is detected automatically.

Runs every 30 minutes (`kg_self_health_check`). Fifteen checks (`_ALL_CHECKS`), each returning `ok` / `warn` / `fail`; a source switched off by configuration is reported as `disabled` and does not raise the overall status. The original six:

1. **`materialization_zero_rate`** — % of rows in `kg_service_health` where a metric is 0 or NULL over 24h. Allowlist for known-zero metrics (`http_5xx_rate`, `p95_latency_ms` while app `/metrics` stays behind JWT — WO-12483).
2. **`sync_lag`** — `max(ts)` per beat task vs expected interval; >2× → warn, >5× → fail.
3. **`anomaly_signal_health`** — count of `kg_anomaly_observations` over 24h. 0 → warn (flat baseline or detector broken). >500 → warn (threshold too loose, overload).
4. **`alerts_resolve_freshness`** — open `kg_alerts` whose `coalesce(last_notified_at, fired_at)` is older than 7 days. >20 → warn. Long-living alerts that Alertmanager still sends are listed separately as `long_firing_alerts`.
5. **`pod_events_link_rate`** — % of `kg_pod_events` over 24h with `service_id NOT NULL`. <80% warn, <50% fail (StatefulSet resolver regression).
6. **`edges_freshness`** — % of `kg_service_edges` with `last_seen_at < 24h` or `NULL`. >30% stale → warn (kg_topology_sync regression).

Added later: `digest_delivery`, `edge_kind_freshness`, `node_freshness`, `source_coverage` (per-source `SourceStatus`, including `cleanup_blocked`), `silent_gaps`, `deploy_stream_ingestion`, `graph_integrity` (phantom DB duplicates, `serves_traffic` self-loops, dangling edges), `orphan_namespaces`, `schema_version`.

Output: audit-log line per run; the snapshot is written to Redis `stats:self_health:last` and exported as metrics (§3f, `docs/METRICS.md` §7); on any `fail`/`warn`, a single Discord embed is posted to `DISCORD_WEBHOOK_SELF_HEALTH_URL` — kept separate from `#infra-error` to avoid drowning operational alerts. A 6-hour dedup window prevents the same canary fail from spamming the channel.

## 3d. Topology Expansion (Wave 7)

Wave 7 расширяет источники топологии KG с двух (env-var heuristic +
Ingress-host externalisation) до пяти, плюс runtime confirmation channel.
Цель — дать confidence-фреймворку (`kg_service_edges.extras.discovery_sources`)
независимые tier-1 источники, чтобы провенанс edges не зависел от единственного
heuristic-парсинга env-переменных.

### Wave 7-X: declarative Service + Ingress parser

`app/knowledge_graph/k8s_topology_resources_sync.py` каждые 15 минут читает
`kubectl get services/ingresses -A -o json` и строит:

- **`serves_traffic`** edge: Service → backing Deployment, по selector-match
  на pod template labels. Declarative замена runtime Endpoint resolution —
  не зависит от живых pods, поэтому работает и для свёрнутых деплоев.
- **`routes_to`** edge: Ingress (как ресурс) → backend Service. Параллельный
  slice к существующему `k8s_ingress_sync.py`, который строит
  `ingress:<host>` → backend как `calls` (host-уровень). Один Ingress
  ресурс часто имеет N hosts/paths — этот модуль покрывает Ingress-as-resource,
  старый — Ingress-as-host. Оба пишутся в `extras.discovery_sources`
  через merge в `populator.upsert_edge`.

RBAC: cluster-role требует `services` + `ingresses` на `get`/`list`/`watch`
(см. `k8s/base/rbac.yaml`).

Включён по умолчанию (нет feature flag) — declarative-источник идемпотентен
и не требует внешних зависимостей кроме `kubectl`.

### Wave 7-Y: PodEvent runtime correlation

`app/knowledge_graph/runtime_correlation.py` каждые 30 минут ищет пары
сервисов, у которых warning-события (BackOff/Unhealthy/OOMKilled/
FailedScheduling/CrashLoopBackOff/FailedMount/ImagePullBackOff) сваливаются
в одном окне `RUNTIME_CORRELATION_WINDOW_MINUTES` (default 15 мин) N+ раз
за `RUNTIME_CORRELATION_LOOKBACK_DAYS` (default 7 дней).

**Что делает.** Подтверждает уже существующие edges новым
`discovery_source = "kg_sync/runtime_corr"` (tier-1 precedence 0.95). Это
дешёвый OTEL-substitute: вместо распределённого трейсинга смотрим на
наблюдаемую кореляцию failure-сигналов.

**Что НЕ делает.** Новые edges из ничего не создаёт. Симметричный сигнал
co-occurrence не определяет направление зависимости — поэтому это
confirmation channel, а не discovery channel. Топология строится
declarative-источниками (env, Service-selector, Ingress, NATS-monorepo).

**Synthetic-исключение.** Synthetic-узлы (NATS-cluster, ingress:host)
исключаются: их pod_events идут через cluster-wide kubelet и дадут
false-positive каждому сервису в namespace. См. `_is_synthetic()`.

Feature flag: `RUNTIME_CORRELATION_ENABLED=true` (включён по умолчанию).

### Wave 7-Z: NATS subjects parser

`app/knowledge_graph/nats_subjects_sync.py` каждые 6 часов клонирует
(shallow + sparse-checkout) WO monorepo, regex-парсит C# исходники на
NATS-consumers (`NatsJetStreamConsumer<T>.Subject => NatsSubjectConst.<NAME>`)
и publish call-sites (`SendToJetStreamAsync(subject: ...)`).

Subject регистрируется как synthetic-Service в namespace `nats-subjects`,
`name = subject:<value>` (например `subject:march-export`). Это
переиспользует существующую схему `kg_services`/`kg_service_edges` без
новых таблиц или миграций.

Для каждого call-site пишется edge `uses_nats` с `extras.direction ∈ {pub, sub}`,
`weight = count(call-sites)`. Это дополняет существующие `uses_nats` к
synthetic NATS-cluster-узлам (env-var-derived) — теперь видно не только
что сервис подключён к кластеру, но и какие именно subjects он публикует
или читает.

Service-name резолвинг: путь `GR.WO.Map.Service/...` → `map-service`
(lowercase, dots→dash) — matches Deployment-имена в k8s.
`NatsSubjectConst.<NAME>` → литеральная строка через один проход по
`GR.Platform/DataBus/Nats/NatsConst.cs`.

Feature flag: `NATS_SUBJECTS_PARSER_ENABLED=false` (выключен по умолчанию —
требует ssh-доступ к gitlab-monorepo и каталог `WO_MONOREPO_PATH`).
Переменные окружения: `WO_MONOREPO_PATH`, `WO_MONOREPO_SSH_URL`,
`WO_MONOREPO_SPARSE_DIRS`.

### Edge kinds (full inventory after Wave 7)

| Edge kind | Producer | Direction semantics |
|---|---|---|
| `calls` | `kg_sync` (env-var URL), `k8s_ingress_sync` (host) | A makes HTTP call to B |
| `uses_nats` (cluster-level) | `kg_sync._extract_nats_clusters` | Service uses NATS-cluster (shared/kingdom) |
| `uses_nats` (subject-level) | `nats_subjects_sync` | Service publishes/subscribes to a subject; `extras.direction ∈ {pub, sub}` |
| `uses_db` | `kg_sync` (secretKeyRef heuristic) | Service uses DB (without reading secret values) |
| `serves_traffic` (Wave 7-X) | `k8s_topology_resources_sync` | Service → backing Deployment (by selector) |
| `routes_to` (Wave 7-X) | `k8s_topology_resources_sync` | Ingress (resource) → backend Service |
| `runs_as_job` (Wave 8-A) | `k8s_jobs_sync` | CronJob/Job → owner Service (через `K8sJob.owner_service_id`, metadata-only) |
| `uses_volume` (Wave 8-B) | `k8s_storage_sync` | Service → PVC (в `kg_volume_edges`) |
| `bound_to` (Wave 8-B) | `k8s_storage_sync` | PVC → PV (в `kg_volume_edges`) |

## 3e. KG Metadata + UX Polish (Wave 8)

Wave 8 — это пакет из 17 PR, который доводит KG-метаданные и Discord
UX до состояния «можно строить Phase A remediation поверх». Контракт
становится формальным, добавляются два больших coverage-блока
(Jobs/Storage), owner inference перестаёт быть prefix-only, появляется
quality_report для baseline-снимков перед remediation-волной.

### Wave 8-A: k8s Jobs/CronJobs coverage (PR #82)

`app/knowledge_graph/k8s_jobs_sync.py` каждые 15 минут читает
`kubectl get jobs,cronjobs -A -o json` и upsert'ит в новую таблицу
`kg_k8s_jobs` (отдельная от `kg_services` — Job/CronJob НЕ "service",
не имеет постоянного pod-а).

- **Закрывает blind spot**: до Wave 8 KG видел только Deployments/
  StatefulSets; alembic migration jobs, backup CronJob-ы (etcd snapshot,
  push-s3), ad-hoc cleanup — оставались невидимыми. Failed alembic при
  rollout = прод stuck на старой схеме, никто не услышит.
- **Поля Jobs**: `succeeded_count` / `failed_count` / `active_count` /
  `completion_time` / `last_pod_exit_code`.
- **Поля CronJobs**: `schedule` / `last_schedule_time` /
  `last_successful_time` / `suspended`.
- **Semantic edge `runs_as_job`**: CronJob → owner Service реализован
  **через `K8sJob.owner_service_id` metadata-column** (не отдельный
  edge-row в `kg_service_edges`). Match через label
  `app.kubernetes.io/part-of` или `app` совпадающий с `kg_services.name`
  в том же namespace. Если matched нет — owner просто NULL.

### Wave 8-B: PVC/PV storage coverage (PR #84)

`app/knowledge_graph/k8s_storage_sync.py` каждые 30 минут (storage
редко меняется — claim создаётся ~раз в неделю, capacity статична, но
phase-переходы Bound→Released важны в течение получаса).

- **Закрывает blind spot**: ClickHouse / Postgres «упал» = в 95%
  случаев диск кончился; до Wave 8 KG не имел никакого storage-слоя.
- **Новые таблицы**: `kg_storage_volumes` (PVC/PV; `kind ∈ {pvc, pv}`;
  PV cluster-scoped — `namespace=''`) + `kg_volume_edges` (heterogeneous
  edges с tagged `src_kind`/`dst_kind`, без FK constraint).
- **Атрибуты**: storage_class, access_modes, phase
  (Bound/Pending/Lost/Released/Available/Failed), capacity_bytes.
- **Edges в `kg_volume_edges`**:
  - `uses_volume` (Service → PVC) — scan всех pod'ов cluster-wide; для
    каждого `pod.spec.volumes[].persistentVolumeClaim.claimName` → edge
    от owning Service (через ownerReference Deployment/StatefulSet/RS).
  - `bound_to` (PVC → PV) через `pvc.spec.volumeName`.
- **disk_pct enrichment** опционален через `STORAGE_METRICS_ENABLED=false`
  (default OFF — kubelet_volume_stats_* scrape config может быть не
  настроен; без него запрос вернёт 0 для всех PVC и замаскирует реальные
  NULL).

### Wave 8-C: multi-signal owner inference (PR #85)

`app/services/ownership_suggester.py` + `app/services/owner_aliases.py`.
Старая prefix-only логика расширена до взвешенного fusion-а трёх
независимых сигналов + manual override:

- **A. Prefix** (weight 0.4) — regex по ns-имени (`squad-N-*`,
  `<env>-kingdom<N>`, bare `monitoring`/`kube-system` → `platform`).
- **B. Deploy history** (weight 0.4) — most-frequent `triggered_by`
  из `kg_deployments` за 30 дней. Username транслируется через
  `owner_aliases.resolve_username` (YAML override + pre-baked defaults).
- **C. Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
  `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
- **Manual override** через `OWNERSHIP_MANIFEST_PATH=ownership.yaml`
  (список `[{ns_pattern, owner, reason}]`, match по glob → confidence=1.0,
  все эвристики игнорируются).

### Wave 8-D: stale_class column (PR #86)

`kg_services.stale_class` (миграция `20260524_0200_add_kg_services_stale_class`)
+ `app/knowledge_graph/stale_classifier.py`:

- `active` — был deploy за последние `ACTIVE_WINDOW_DAYS` (default 30d).
- `expected_stale` — давно не катился, но это норма: backup/cron/system
  (`*-backup`, `*-cron`, ns `kube-system`, `monitoring`) либо
  infra/platform owner вне ACTIVE_WINDOW_DAYS.
- `suspicious_stale` — нет deploys 30d, не expected_stale.

`kg_sync.sync_namespace` переписывает column идемпотентно на каждом
sync; `stats_digest.stale_deployments_section` читает column как
primary с fallback на legacy `_classify_stale` (старая инсталляция без
свежего sync).

### Wave 8-E: KG schema/quality contract (PRs #83 / #89)

`app/knowledge_graph/contract.py` — единственный источник истины:

- `KG_SCHEMA_VERSION = "2.9"` (Wave 7 → 2.1; PR #82/#84/#86 → 2.2; orphan на
  app-scope → 2.3; `node_kind` → 2.4; orphan перестал считать `serves_traffic`
  связностью → 2.5; `owner_source` → 2.6; `stale_class = gone` → 2.8;
  владелец namespace `kg_namespaces.owner_*` → 2.9). История и мотивация
  каждого шага — `docs/KG_SCHEMA_CONTRACT.md` §1.
- `EDGE_KINDS` — реестр всех edge kinds + spec (semantic / src_kinds /
  dst_kinds / source / status / table). Поле `table` различает где
  edge живёт: `kg_service_edges` / `kg_volume_edges` / `fk_only` (через
  FK) / `metadata_only` (через owner_service_id).
- `OWNER_SOURCES` / `OWNER_SOURCE_ALIASES` — canonical источники
  owner-а + маппинг коротких имён из `ownership_suggester`.
- `STALE_CLASS_VALUES` — enum значений `kg_services.stale_class`.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика: сверяет
  реальные kinds в БД с реестром, логирует drift.
- **Gate #22** (`tests/test_contract_drift.py`) — auto-validation что
  код и контракт не разъезжаются (CI-блокирует).

Человекочитаемая версия с quality metrics, compatibility policy и
edge inventory — `docs/KG_SCHEMA_CONTRACT.md`.

### Wave 8-F: Discord UX polish (PRs #76, #77, #79, #81, #90)

- **#76**: blast radius (X) / NATS impact (Z) / pod trail (Y) рендерятся
  в enriched embed как отдельные поля — до этого Wave 7 наполнял KG,
  но embed его не показывал.
- **#77**: PATCH-dedup для `send_enriched_alert` — 30-минутное content-key
  окно: если тот же логический алерт приходит повторно, PATCH-им
  существующее сообщение через `webhook?wait=true`. Параллельно к
  существующему fingerprint-dedup.
- **#79**: human-time (`2 hours ago` вместо ISO), pod name, ready/desired
  replicas, kubelet reason. Новый модуль `app/utils/time_human.py`.
- **#81**: stats digest UX — series→series collapse (один сервис с
  burst-pattern не разрывает digest), unowned action block (suggest owner
  через ownership_suggester), `trend Δ24h` для всех метрик, отдельные
  секции `chronic`/`resurfaced`, `stale classification` pill, переименование
  `fragile → blast-radius`.
- **#90**: cascade deploys aggregation (один push в `prod-shared` cascades
  на N kingdom realms → считаем как один деплой), new-baseline placeholder
  `(new baseline)` для Δ24h когда yesterday-state не существует.

### Wave 8-G: quality_report + snapshot fixtures (PRs #87, #88)

- **`app/scripts/quality_report.py`** — идемпотентный read-only CLI:
  считает 5 групп метрик из Postgres KG (services / edges / events /
  deploys / coverage). Markdown (default) или JSON output. Use case —
  точка отсчёта для Phase A (remediation), чтобы demonstrably улучшать
  metrics, а не угадывать. Baseline-снимок 2026-05-24:
  `docs/quality_report_baseline_2026_05_24.md`.
- **`tests/fixtures/discord_snapshots/`** — 7 known-good embed cases
  (critical_fresh / critical_resurfaced / warning_compact /
  burst_aggregation / daily_digest / chronic_digest / team_digest) с
  input.json + expected.json. **UX regression-guard**: любое изменение
  в discord/embed_builder валит diff. Update workflow:
  `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py`.

## 3f. Evidence & Incident Layer (v1.0.4 – v1.0.8)

The deep review of 2026-09-05 (10 PRs, then a roadmap series) changed what the graph *means*, not only what it stores. Contracts live in [`SEMANTIC_CONTRACT.md`](SEMANTIC_CONTRACT.md) §5.1 and §11–§13.

### SourceStatus — a sync that saw nothing is not a sync that ran
Every data-source task returns `SourceStatus` ∈ `success / partial / empty / unavailable / failed / invalid` (`app/knowledge_graph/source_status.py`). Only `success` / `partial` write the beat heartbeat; the status itself is always written to Redis (`stats:beat:last_run:status:<task>`) and shown by the `sync_lag` self-health check as `last_status`. A source switched off by configuration reports `disabled` instead of `warn`. Guard: `tests/test_data_sources_are_watched.py::test_every_data_source_declares_its_status`.

### Evidence contract — three outcomes, not two
`Fact.verdict` ∈ `found / absent / unknown`. `unknown` is produced only by `Fact.unknown(kind, reason)` (confidence forced to 0, `epistemic=unknown`, `unknown_reason` mandatory). Every `Rule` declares `sources` — the ctx fields it depends on — and `Rule.run()` demotes `absent` to `unknown` when any of them is listed in `ctx["source_status"]`; `found` is never demoted. `alert_enrichment.enrich_alert` fills `source_status` on query exceptions, on a stale deploy stream (`deploy_stream_freshness().stale`, now also in the per-service path) and when the service is not in the graph. Consumers must distinguish: `absent` may refute a hypothesis, `unknown` must not (`fact_critic` skips unknown anchors; `to_prompt_context()` renders `?` with a warning). Deploy attribution became evidence: `recent_deploys_for*` return `attribution_scope` (`service` / `namespace` / `unknown`) — an exact record is `observed 0.95`, an ns-broadcast `inferred 0.6`.

### Workload identity — `k8s_uid` and `incarnation`
`kg_services.k8s_uid`, `incarnation`, `incarnation_changed_at`: a Deployment re-created under the same name is a different object; `incarnation` increments inside `ON CONFLICT` when the uid changes. `TargetRef.uid / incarnation` carry it into remediation.

### `kg_deploy_watch` — rollouts seen by the cluster
The second source of `kg_deployments`: a Redis snapshot of every workload (`uid`, `template_hash = sha256(spec.template)`, images); a rollout is a change of images or template hash — **not** `metadata.generation`, which HPA and annotation writers bump every tick (the 1.0.4 hotfix: 10 064 false rows on 776 services in 40 minutes). Rows land on the service node with `namespace_scope=false`, so `stale_class='active'` became reachable (0 → 77 services).

### Incident — one open incident per service
`kg_incidents` (`app/knowledge_graph/incidents.py`): `attach_alert` joins a firing alert to the open incident of its service, re-opens one resolved less than 30 minutes ago (`reopened_count`), or opens a new one; the invariant «≤ 1 open per (namespace, service)» is a **partial unique index**, not application code. `kg_alerts.incident_id` holds the `incident_key` (`ns/service@time`). `reconcile_incidents` (beat) resolves when every alert is resolved and ages out after 7 silent days. `noise=true` when enrichment classified all of the incident's alerts as noise (gen-mismatch / meta / rollout, per-alert kinds in `extras.noise_fingerprints`); a real alert clears the flag; `GET /kg/incidents` hides noise unless `include_noise=true`. Cross-service correlation is deliberately out of scope until a month of data exists — it will be edges between incidents, never a relaxation of the invariant.

### Timeline — six tables on one axis, plus what the copilot did
`incident_timeline.build_timeline`: incident, alerts, deployments (with attribution as evidence), pod events, anomalies (bucketed per metric·hour, always `inferred`), error-level logs in `[opened − 60 min, (resolved | now) + 30 min]`, sorted by time then causal kind order. **Operational memory** (v1.0.8) continues the same axis from `incidents.analysis` and `kg_remediation_decisions` joined by fingerprint: `evidence` (facts by verdict), `diagnosis` (`inferred`), `decision` (`declared`), `action.applied` (`observed`), `verification` (`observed` for verified/failed, `unknown` for pending); `memory.outcome` distinguishes `action_verified`, `action_applied_unverified`, `resolved_without_action`, `resolved_without_analysis`. `unknowns` names sources that could not be queried — an empty lane is never silently «nothing happened».

### Blast radius — who breaks if X breaks
`blast_radius.blast_radius_v2`: BFS *against* `calls` / `uses_db` / `uses_nats` / `serves_traffic` up to two hops; every edge is classified by `epistemic.classify_edge` (sources ∪ `discovered_by`, freshness, contradictions such as `serves_traffic` with `endpoints_ready == 0`); an impacted node inherits the weakest link of its path, `contradicted` anywhere marks the whole path. `unknowns`: no `calls` edges at all («unknown, not absent»), callers known only from manifests/env (all `calls` in the graph are declared/inferred — no runtime observation yet), `inactive` edges skipped, horizon, contradicted paths. Legacy `blast_radius_for` keys are preserved for the Discord embed, which now shows an epistemic legend (✓ ◇ ≈ ⌛ ⚠).

### Remediation verification — same target, took effect, got better
`remediation/verification.py`: before the write `snapshot_target` reads the live object (`uid`, `generation`, `observedGeneration`, `sha256(spec.template)`, replicas) through the read-only `kubectl get` path of `kubectl_breaker.run_kubectl`; if the graph's `target_ref.uid` differs, `apply_intent` refuses (`target_reincarnated`). A second snapshot follows the write; both live in `executor_applied`. `schedule_verification` enqueues `remediation_verify` at +5 and +15 minutes (`REMEDIATION_VERIFY_DELAYS_SEC`); `assess()` checks identity, effect (template/generation for restart, replicas for scale), convergence, health, alert resolution and new CrashLoop/OOM pod events; outcome → `executor_state` `verified` / `verification_failed`; every check is `True / False / None`, and `None` means «could not check», never «ok».

### Self-health in metrics
`SelfHealthCollector` exports `copilot_self_health_status`, `copilot_self_health_check_status{check}`, `copilot_self_health_last_run_timestamp` from the Redis snapshot; `VMPodScrape` + `VMRule sre-ai-copilot` (`k8s/monitoring.yaml`) alert on stale/absent/failing/warn-stuck checks. Counters `diagnostic_facts_verdict_total{kind, verdict}` and `remediation_verification_total{outcome}` measure Known Unknowns and verification outcomes directly.

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

- Celery retries are configured on critical tasks; every beat task holds a `single_instance` lease; heavy syncs run on their own queue (§2.2).
- `/readyz` checks PostgreSQL availability with `SELECT 1`.
- Prometheus metrics: API on :8001 (request latency, webhook counters, self-health and queue-length collectors), workers on :8001 in multiprocess mode (`app/observability/worker_metrics.py`); scraped by `VMPodScrape sre-ai-copilot`, alerted on by `VMRule sre-ai-copilot` (`k8s/monitoring.yaml`). Inventory — `docs/METRICS.md` §7.
- OpenTelemetry tracing is initialized at startup when `OTLP_EXPORTER_ENDPOINT` is set.
- Logging — §2.7; audit log — JSON lines to stdout (a file only if `AUDIT_LOG_PATH` is set).
- Self-health, weekly reconciliation, retention, backup — §2.5–§2.6.

## 8. Security

- JWT-based user dependency for `/copilot`.
- `ALERTMANAGER_WEBHOOK_SECRET` HMAC validation for Alertmanager webhook endpoints; `REMEDIATION_WEBHOOK_SECRET` (HMAC over `timestamp.body`, anti-replay) for `/webhooks/remediation`.
- Discord interactions: Ed25519 signature check with `DISCORD_PUBLIC_KEY`; clicks arrive via the ai-reviewer relay, admitted by NetworkPolicy from `ai-reviewer-net` only (§2.3).
- Cluster writes only from `copilot-executor` under its own ServiceAccount `sre-ai-executor`: Role `sre-ai-remediate` (`deployments`, `deployments/scale`: get/list/patch/update) is bound per namespace to that SA only (`k8s/base/rbac.yaml`); API and workers run as `sre-ai` with read-only access.
- Guardrails at the DSL level (`ExecutionIntent`) and k8s policy validator (`K8sSecurityGuard`).
- Approval API for human-in-the-loop before any write action.
- `SAFE_MODE=true` is enforced in production (config validator raises on `SAFE_MODE=false` + `ENV=production`).
- Prompt injection guard (`prompt_guard.detect_injection`) with input length cap (`PROMPT_INPUT_MAX_CHARS`).
- **PII redaction** (`app/services/pii_redaction.py`) — write-time scrubbing of Seq log samples and defense-in-depth scrubbing of Discord embed strings. Patterns: emails, IPv4/IPv6, JWT, bearer tokens, UUIDs, long hex, `password|token|secret|api_key` key/value pairs.
- **Approve / Decline authz** on Discord buttons — `DISCORD_APPROVERS_USER_IDS` / `DISCORD_APPROVERS_ROLE_IDS` allowlists (production uses the user list only), `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` quota. Fail-closed when both allowlists are empty.
- **Dedicated read-only Postgres role** (`kg_reader`) for any external KG access — separate user, `SELECT` on `kg_*` and `alembic_version` only, no default privileges (future tables must be granted explicitly).

## 9. External Integrations

| Integration | Purpose | Config keys |
|---|---|---|
| Kubernetes (MCP) | Pod state, logs, events, deployment control | `TEAMCITY_MCP_URL` (via wo-tools) |
| VictoriaMetrics | Memory/CPU metrics window before incident | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Recent deploy context | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Known open/resolved tickets for the service | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Alert cards, digests | `DISCORD_WEBHOOK_URL` |
| Discord (bot) | Proposal cards with buttons, interactions | `DISCORD_BOT_TOKEN`, `DISCORD_INCIDENT_CHANNEL_ID`, `DISCORD_PUBLIC_KEY` |
| Discord (per-team) | Per-team channel routing for digests/incidents | `DISCORD_TEAM_CHANNEL_MAP` (JSON) |
| Discord (self-health) | KG canary alerts, separate from `#infra-error` | `DISCORD_WEBHOOK_SELF_HEALTH_URL` |
| Discord (authz) | Allowlist for Approve/Decline buttons | `DISCORD_APPROVERS_USER_IDS`, `DISCORD_APPROVERS_ROLE_IDS`, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` |
| Seq | Application log stream → `kg_log_observations` | `SEQ_INSTANCES` or `SEQ_URL_<ENV>` / `SEQ_TOKEN_<ENV>` |
| squad-medic (external) | Remediation run events → `kg_remediation_events` | `REMEDIATION_WEBHOOK_SECRET` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
| Anomaly tuning | Robust-z thresholds | `KG_ANOMALY_ROBUST_Z_WARN`, `KG_ANOMALY_ROBUST_Z_CRIT` |
