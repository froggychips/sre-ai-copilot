# SRE AI Copilot — Metrics Pipeline

Last updated: **2026-06-10**.

Map of the metrics flow: which signals exist in VictoriaMetrics, which of them are materialised into the Knowledge Graph, by which sync, and — just as important — which signals are **absent** and what their zeros actually mean.

## 1. Signal Map (TL;DR)

| Signal | KG table | Source | Status |
|---|---|---|---|
| `cpu_pct` / `mem_pct` / `restarts_rate` (per service) | `kg_service_health` | `kube_*` / cAdvisor via VMNodeScrape | ✅ live |
| `http_5xx_rate` / `p95_latency_ms` (per service) | `kg_service_health` | ASP.NET app `/metrics` | ❌ always 0 — blocked by WO-12483 |
| `p95` / `p99` / `rps` / `error_4xx_rate` / `error_5xx_rate` (per ingress endpoint) | `kg_ingress_observations` | `nginx_ingress_controller_*` via VMPodScrape | ✅ live since 2026-06-10 |
| Anomalies (robust-z) | `kg_anomaly_observations` | derived: `kg_service_health` + `kg_log_observations` | ✅ live |
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

### `kg_ingress_observations` ← `ingress_observations_sync.py` (beat: `kg_ingress_observations_sync`, ~10 min)

Per `(ingress_name, host, path)`: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_4xx_rate` / `error_5xx_rate` from `nginx_ingress_controller_*`. The host/path list comes from `kubectl get ingresses -A` (reusing the `k8s_ingress_sync` parser); the backend service is resolved into `service_id` via the ingress backend → `kg_services` (namespace + name); if unresolved, the row is still written with `service_id = NULL`. Rows where all metrics are 0 are skipped (endpoint not covered by the exporter). Idempotent on `UNIQUE(ingress_name, host, path, ts)`.

This is the **endpoint-level** slice (per host/path) — a different cut than the per-service one in `kg_service_health`. It is currently the only source of real HTTP 5xx / latency in the KG.

### `kg_anomaly_observations` ← `anomaly_detection.py` (beat: `kg_anomaly_detect`)

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
