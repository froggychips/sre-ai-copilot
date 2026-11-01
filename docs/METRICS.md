# SRE AI Copilot — Metrics Pipeline

Last updated: **2026-11-01** (v1.0.33). §1–§5 describe the KG metrics pipeline, §6 graph-quality metrics, §7 the copilot's own Prometheus metrics and alerts.

Map of the metrics flow: which signals exist in VictoriaMetrics, which of them are materialised into the Knowledge Graph, by which sync, and — just as important — which signals are **absent** and what their zeros actually mean.

## 1. Signal Map (TL;DR)

| Signal | KG table | Source | Status |
|---|---|---|---|
| `cpu_pct` / `mem_pct` / `restarts_rate` (per service) | `kg_service_health` | `kube_*` / cAdvisor via VMNodeScrape | ✅ live |
| `http_5xx_rate` / `p95_latency_ms` (per service) | `kg_service_health` | ASP.NET app `/metrics` | ❌ always 0 — blocked by WO-12483 |
| `p95` / `p99` / `rps` / `error_4xx_rate` / `error_5xx_rate` (per ingress endpoint) | `kg_ingress_observations` | `nginx_ingress_controller_*` via VMPodScrape | ✅ live since 2026-06-10 |
| Anomalies (robust-z) | `kg_anomaly_observations` | derived: `kg_service_health` + `kg_log_observations` | ✅ live |
| `orleans_latency_avg_ms` / `orleans_timedout_rate` / `orleans_messaging_fault_rate` / `orleans_pings_missed_rate` / `orleans_activation_churn` / `orleans_rerouted_rate` (per grainhost service) | `kg_service_health` | `microsoft_orleans_*` via `VMPodScrape town-grainhost` (chart, port 8080) | ✅ live where the chart landed (28 ns on 2026-09-07; prod pending) — silo health, **not HTTP RED**; latency is an average (no histogram) |
| `log_error_rate` | `kg_log_observations` | Seq REST API | ✅ live (log proxy, not HTTP) |
| `health_score` | `kg_services.health_score` | composite of KG signals | ✅ live (infra proxy, not user-facing) |

## 2. Sources in VictoriaMetrics

Stack: vm-operator in ns `monitoring`, VMAgent with `selectAllByDefault` — any `VMServiceScrape` / `VMPodScrape` / `VMNodeScrape` object in any namespace is picked up automatically.

- **`kube_*` / cAdvisor / kubelet** — VMNodeScrape objects shipped by the kube-stack chart. Cover the **entire cluster**: per-pod CPU, memory, restarts. This is the backbone of `kg_service_health`.
- **PostgreSQL exporters (bitnami) + NATS exporters** — `VMServiceScrape` objects in application namespaces, auto-created by the charts (~518 objects cluster-wide). Picked up by VMAgent without any manual config.
- **nginx-ingress controllers** — `VMPodScrape` objects `ingress-nginx-shared` and `ingress-prod` (ns `cattle-system`, `honorLabels: true`). Metrics were enabled on **2026-06-10** via the `--enable-metrics=true` flag on both DaemonSets. **`metrics-per-host` MUST stay enabled** — the `host` label is what `ingress_observations_sync` keys its PromQL on; disabling it silently kills `kg_ingress_observations`.
- **ASP.NET application metrics — ABSENT.** The app `/metrics` endpoint (Kestrel) sits behind JWT middleware and returns 401 to the scraper. Backend ticket: **WO-12483**. Once rolled out, the remaining work is a `VMServiceScrape` per app namespace (the metric needs a `service` label for `metrics_sync` to match it). Until then, `http_requests_total` / `http_request_duration_seconds_bucket` do not exist in VM for app namespaces.

## 3. What Lands in the KG, and by Which Sync

### `kg_service_health` ← `metrics_sync.py` (beat: `kg_metrics_sync`, ~10 min)

Per-service `cpu_pct` / `mem_pct` / `restarts_rate` from `kube_*` / cAdvisor. Namespace-aggregated PromQL (5 queries per namespace instead of 5 per service — ~385 queries total vs the old ~12300); pod → service resolution by longest-prefix match against known service names. Aggregation across pods of one service: cpu/mem — mean, restarts — sum. Fully-zero rows are not inserted (exporter does not cover the service). Idempotent on `UNIQUE(service_id, ts)`.

`http_5xx_rate` and `p95_latency_ms` columns exist and the PromQL is valid, but they are **always 0 until WO-12483** — see §2.

### `kg_service_health.orleans_*` ← `metrics_sync.py` (same beat, v1.0.9)

Discovery once per tick — `count by (namespace) (microsoft_orleans_orleans_app_requests_latency_count{job=~".+/.+"})` — then seven extra PromQL only for namespaces that have the Orleans meter (28 of 231 on 2026-09-07), grouped `by (pod)` and mapped to services like cpu/mem. **Pull series only** (`job=~".+/.+"`, since 1.0.10): the same meter is also pushed to push-gateway (ns `monitoring`), where series of every kingdom collapse into `push-gateway-*` pods with the pusher's own `job` name — without the filter discovery picked up `monitoring` and the sum of all silos (prod included) was written as the health of a `push-gateway` service (52 s latency, 36 M faults/min). VM-operator sets `job = <ns>/<scrape>` for pull, so the selection is positive: if the format ever changes, Orleans disappears entirely (`orleans_namespaces=0` in stats) instead of turning into garbage. Latency is call-weighted across pods (`Σ rate(sum) / Σ rate(count)`, ms); the four rates are per minute: `app_requests_timedout`, messaging delivery faults (`rejected|expired|sent_failed|sent_dropped`; `messaging_rerouted` is excluded since 1.0.10 — a forward to another silo is a cost, not a failure, and at 10–14 k/min in preprod it hid the 11.9 k/min of real faults in squad-14), `messaging_pings_reply_missed` (death-vote proxy), activation churn (`catalog_activation_created|destroyed|shutdown`), and since 1.0.11 `orleans_rerouted_rate` (`messaging_rerouted` — forwards to another silo, kept apart from faults on purpose: they climb before faults do when a silo dies or a rollout moves activations). **Zero semantics:** prometheus-net does not export a counter until its first increment, so a missing fault series on a service that *has* `latency_count` is written as `0.0` (no faults) — `NULL` means the service has no silo at all. The anomaly detector scans these columns like the rest (`MIN_ABS_SPREAD_BY_METRIC`: 50 ms, 0.5, 1.0, 0.5, 50/min, 100/min); critical embeds get a «🧬 Orleans silo» field with the latest value, the 24 h baseline and ⚠ for growth above +50 %. Consumers: `queries.orleans_health_for`.

### `kg_ingress_observations` ← `ingress_observations_sync.py` (beat: `kg_ingress_observations_sync`, ~10 min)

Per `(ingress_name, host, path)`: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_4xx_rate` / `error_5xx_rate` from `nginx_ingress_controller_*`. The host/path list comes from `kubectl get ingresses -A` (reusing the `k8s_ingress_sync` parser); the backend service is resolved into `service_id` via the ingress backend → `kg_services` (namespace + name); if unresolved, the row is still written with `service_id = NULL`. Rows where all metrics are 0 are skipped (endpoint not covered by the exporter). Idempotent on `UNIQUE(ingress_name, host, path, ts)`.

This is the **endpoint-level** slice (per host/path) — a different cut than the per-service one in `kg_service_health`. It is currently the only source of real HTTP 5xx / latency in the KG.

### `kg_anomaly_observations` ← `anomaly_detection.py` (beat: `kg_anomaly_detection_task`, ~10 min)

Robust-z (median + MAD, seasonal baseline) over the materialised series: `cpu_pct` / `mem_pct` / `restarts_rate` from `kg_service_health`, plus `log_error_rate` derived from Seq logs (`kg_log_observations`). Anomaly detection on a flat-zero metric (e.g. `http_5xx_rate`) simply produces no observations — graceful degradation, no false positives.

## 4. Zero Semantics and Proxies (Critical for Consumers)

The same zero means opposite things in different tables. Any consumer (digest, RCA, LLM pipeline, humans) must read this section:

- **`kg_ingress_observations.error_5xx_rate = 0` with `rps > 0`** → genuinely no errors. Traffic is flowing, the metric is alive, 5xx really is zero.
- **`kg_service_health.http_5xx_rate = 0`** → **NO DATA**, not "no errors". App metrics are not scraped (WO-12483). Same for `p95_latency_ms`.
- **`health_score`** is an **infra proxy**: it is derived from alerts, pod_events, deploy/SLO aggregates — not from user-facing latency or errors. A high score means "CPU/events are fine", **not** "no 5xx".
- **`log_error_rate`** is a **log-derived proxy** (Seq Error-level lines), not HTTP 5xx. Useful as an application-error signal, but not interchangeable with request error rate.

## 5. Verifying the Flow (Operational Cheat-Sheet)

Which namespaces are emitting ingress metrics into VM:

```promql
# against http://vmsingle-vm-victoria-metrics-k8s-stack.monitoring.svc:8428
count(nginx_ingress_controller_requests) by (namespace)
```

Whether observations reach the KG and how fresh they are:

```sql
SELECT count(*), max(ts) FROM kg_ingress_observations;
```

Whether the scrape objects are still in place:

```bash
kubectl -n cattle-system get vmpodscrape
# expect: ingress-nginx-shared, ingress-prod
kubectl -n cattle-system get ds -o yaml | grep -- --enable-metrics
# expect: --enable-metrics=true on both controller DaemonSets
```

---

## 6. Graph quality metrics — what they do NOT count

Updated: **2026-08-08**.

Three metrics (`orphan_pct`, `owner_pct`, `app_scope`) are computed in
`contract.compute_orphan_stats` / `STARTUP_CONTRACT_CHECK` and surface in the
digest. All three have non-obvious exclusions — without them the numbers move
when the schema changes, not when the infrastructure does.

### Only `node_kind='service'` is counted

Since contract 2.4 `kg_services` holds three node types. Quality metrics count
**logical services only**. Workload nodes (backing
Deployment/StatefulSet/DaemonSet) are excluded: there are 2871 of them against
8669 services, and including them would double the denominator. In practice it
would look like owner coverage collapsing from 99.5% to ~50% on rollout day —
a regression that never happened.

### `orphan` does not count `serves_traffic`

That edge links a Service to its own backing workload — a node to its own
implementation, not to another service. Before node types existed it
degenerated into a self-loop and was discarded; with `node_kind` it became
real and appeared at once for every service with a selector.

Measured in production on 2026-08-08, right after rollout:

| counting | orphan |
|---|---|
| any edge | 2072 / 4933 → 42.0% |
| excluding `serves_traffic` (current) | 3578 / 4933 → **72.5%** |

The second row matches the pre-rollout value: inter-service connectivity did
not change at all. The first version of the metric would have reported a
twofold improvement that never occurred.

**Rule:** a quality metric must not improve because the storage schema
changed. When adding a new node or edge type, re-examine every metric that
counts "anything at all", not only the ones where you expect an effect.
Locked down by `test_serves_traffic_alone_does_not_clear_orphan` and
`test_compute_orphan_stats_ignores_workload_nodes`.

### What the current value means

`orphan_pct ≈ 72%` does NOT mean "the graph is broken". The denominator is
real services minus `expected_stale` infrastructure across all environments,
including dev/preprod where topology is knowingly incomplete. A missing edge
means "the relationship is unknown", not "there is no relationship" (see §7 in
`KG_SCHEMA_CONTRACT.md`).

## 7. Copilot's own metrics (inventory, v1.0.33)

Updated: **2026-11-01**. Every name below was checked against `app/` (`prometheus_client` `Counter`/`Histogram` and the custom `Collector`s).

### Where they are exported

| Process | Endpoint | Scraped by `VMPodScrape sre-ai-copilot` |
|---|---|---|
| `sre-ai-api` | `:8001/metrics` (`start_http_server` in `app/main.py`), plus the two Collectors below | yes |
| `copilot-worker`, `copilot-worker-sync` | `:8001/metrics`, multiprocess mode (`app/observability/worker_metrics.py`, `PROMETHEUS_MULTIPROC_DIR`) | yes |
| `copilot-executor` | the same worker server starts (the manifest sets `PROMETHEUS_MULTIPROC_DIR`), but the pod declares no `metrics` port | **no** — counters incremented there (`pipeline_executor_applied_total`, `remediation_attempt_transitions_total` from `apply_intent`) are not collected |
| `copilot-beat` | no server | listed in the selector, stays an empty target |

NetworkPolicy admits :8001 only from ns `monitoring`.

### Collectors (computed at scrape time, API process only)

| Metric | Type | Meaning |
|---|---|---|
| `copilot_self_health_status` | gauge | Overall verdict of the last self-health run: 0 ok, 1 warn, 2 fail, 3 unknown |
| `copilot_self_health_check_status{check}` | gauge | Per-check verdict (15 checks: `sync_lag`, `source_coverage`, `graph_integrity`, `schema_version`, …); `disabled` sources are excluded from the worst-of |
| `copilot_self_health_last_run_timestamp` | gauge | Unix time of the snapshot in Redis (`stats:self_health:last`) |
| `celery_queue_length{queue}` | gauge | Pending tasks per broker queue (`LLEN`): `celery`, `sync`, `executor`. Redis unavailable → no series (not zero). Since 1.0.28; before that it was a Gauge nobody set |

No snapshot / no Redis → the series are absent, not zero.

### API process

| Metric | Type | Meaning |
|---|---|---|
| `request_duration_seconds` | histogram | HTTP request latency (middleware) |
| `alerts_suppressed_total{reason, alertname}` | counter | Alerts dropped before a card: `allowlist`, `self_noise`, `inhibited_warn`, `repeat` (Alertmanager repeat of a still-firing alert, 1.0.31), `controller_lag_rancher_churn` (GenerationMismatch churn on `/store`) |
| `alertmanager_alerts_truncated_total{endpoint, receiver}` | counter | Alerts Alertmanager cut by `max_alerts` (`truncatedAlerts` in the payload) — part of the group never reached the copilot |
| `webhook_replay_rejected_total{source}` | counter | Signed webhook requests rejected as replays (`local` cache / `shared` Redis store) |
| `webhook_replay_shared_store_errors_total` | counter | Redis nonce-store failures — anti-replay degraded to per-process |
| `rate_limit_redis_errors_total{limiter}` | counter | Rate-limit checks that could not reach Redis |
| `rate_limit_local_fallback_total{limiter, decision}` | counter | Decisions of the in-process fallback limiter |

### Worker processes — diagnostics, remediation, LLM layer

| Metric | Type | Meaning |
|---|---|---|
| `diagnostic_facts_verdict_total{kind, verdict}` | counter | Facts by evidence verdict — the `unknown` share per kind is the direct measure of Known Unknowns |
| `diagnostic_facts_observed_total{kind, observed}` | counter | Legacy two-state counter, kept for dashboards |
| `remediation_verification_total{outcome}` | counter | Outcomes of `remediation_verify`: `verified` / `failed` / `pending` / `unknown` |
| `remediation_attempt_transitions_total{from_status, to_status}` | counter | Status transitions in `kg_remediation_attempts` |
| `pipeline_executor_applied_total{success}` | counter | Real apply outcomes after approval |
| `pipeline_stage_duration_seconds{stage}` | histogram | Per-stage latency |
| `pipeline_executor_status_total{status}`, `pipeline_execution_intent_total{parsed, action}`, `pipeline_resolution_quality_total{quality}`, `pipeline_incident_recurrence_total{is_recurrence}`, `pipeline_incident_flapping_total`, `pipeline_fact_conflicts_total{kind_a, kind_b}`, `pipeline_no_survivor_total`, `pipeline_hypotheses_count`, `pipeline_survivors_count` | counters / histograms | LLM pipeline outcomes |
| `hypothesis_generated_total{perspective}`, `hypothesis_grounded_total{perspective}`, `hypothesis_refuted_total{by}`, `hypothesis_disagreement_total` | counters | Multi-hypothesis / FactCritic |
| `llm_request_duration_seconds{model}`, `llm_api_errors_total{model, error_type}` | histogram / counter | Per-backend LLM latency and errors (`app/core/tracing.py`) |
| `llm_tokens_per_agent_total{agent, model, direction}`, `llm_latency_per_agent_seconds{agent, model}`, `llm_errors_per_agent_total{agent, model, error_type}` | counter / histogram / counter | Per-agent usage |
| `llm_cost_usd_total{model}` | counter | Estimated LLM spend |
| `llm_budget_denied_total{reason}` | counter | Budget guard refusals (ceiling not set / exhausted / counter state unknown) |
| `prompt_input_truncated_total`, `prompt_input_chars` | counter / histogram | Prompt inputs truncated before the model call; input size before truncation |

Everything under «pipeline», «hypothesis», «llm», «prompt» is produced only by the LLM pipeline. With `LLM_PIPELINE_ENABLED=false` in production these series stay empty; a flat zero there is not a health signal. `llm_tokens_total{model, type}` is declared in `app/observability/ai_metrics.py`, but nothing increments it (its helper `track_llm_metrics` has no callers).

### Alerts (`VMRule sre-ai-copilot`, `k8s/monitoring.yaml`)

| Alert | Condition | Severity |
|---|---|---|
| `CopilotSelfHealthStale` | snapshot older than 1 h, for 15 m | warning |
| `CopilotSelfHealthAbsent` | `copilot_self_health_last_run_timestamp` absent for 30 m | warning |
| `CopilotSelfHealthFailing` | any check `>= 2` for 30 m | warning |
| `CopilotSelfHealthWarnStuck` | any check `== 1` for 24 h | warning (was info until 1.0.28 — no path posted it) |
| `CopilotApiDown` / `CopilotWorkerDown` / `CopilotWorkerSyncDown` | 0 available replicas of `sre-ai-api` / `copilot-worker` / `copilot-worker-sync` for 5 m | critical |
| `CopilotExecutorDown` | 0 available replicas of `copilot-executor` for 5 m | warning |
| `CopilotPodCrashLooping` | restarts in ns `sre-ai` for 15 m | warning |
| `CopilotPostgresBackupFailed` | failed `postgres-backup-*` Job | warning |
| `CopilotPostgresBackupMissing` | no successful `postgres-backup` for more than 26 h (or no data) | warning |
| `CopilotCeleryBacklog` | `max by (queue) (celery_queue_length) > 100` for 15 m | warning |

