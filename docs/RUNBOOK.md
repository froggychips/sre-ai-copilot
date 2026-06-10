# SRE AI Copilot — Runbook & Combat Run History

This document records the end-to-end test runs ("combat runs") used to validate and improve the accuracy of the diagnostic pipeline. Each run documents the alert that was injected, what the pipeline observed, what problem was found, what fix was shipped, and the outcome.

---

## Pipeline Overview (quick reference)

```
AlertManager webhook
  ↓
DiagnosticsEngine     ← rule-based FactStore (oom_killed, process_crash, crashloop, …)
  ↓ fact_conflicts?  ← MUTUALLY_EXCLUSIVE_PAIRS cap → evidence.conflict_with
MultiHypothesisAgent  ← fan-out: app / infra / deps / runtime* (* precondition: process_crash)
  ↓ PERSPECTIVE_PRECONDITIONS filter
FactCriticAgent       ← adversarial grounding — each hypothesis vs FactStore
  ↓ survivors
FixAgent              ← generates ExecutionIntent JSON
  ↓ is_recurrence? / jira_context?
RiskAgent             → Discord approval flow
  ↓
KnowledgeGraph        ← only stores quality causes (_is_quality_cause filter)
SimilarIncidentEngine ← recurrence detection (7-day window)
```

---

## Run 1 — Smoke Test (OOM false positive baseline)

**Date:** 2026-05-12  
**Incident ID:** `e2e-smoke-4c30c313d831`  
**Alert:** CrashLoopBackOff — pod exiting with code 139 (SIGSEGV)  
**What was injected:** AlertManager webhook with a synthetic SIGSEGV pod alert.

### Observed

```
facts_observed: ['oom_killed']
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['oom_killed']. Manual triage required."
resolution_quality: unresolved
```

### Problem identified

`OOMKilledRule` was firing on text-regex match: it found the string "OOMKilled" in namespace-wide pod events even though the *target pod* had exit code 139 (not 137 = OOM). The false-positive `oom_killed` fact was then the only observed fact, so all hypotheses failed grounding.

Additionally, "Manual triage required" was being stored as the resolved `cause` in the Knowledge Graph, which would later pollute the SimilarIncidentEngine with garbage.

### Fix shipped

| Item | Change |
|---|---|
| `OOMKilledRule` structured gate | If target pod has a non-OOM exit code (≠ 0, ≠ 137) in `k8s_pod_state`, return `observed=False` immediately — skip text-regex fallback |
| KG quality gate | `_is_quality_cause()` rejects `None` and strings starting with "No hypothesis survived…" or "Manual triage required" |
| `analysis["cause"] = None` | When no hypothesis survives, store `None` (not the error string) |

---

## Run 2 — Smoke Test (conflict detection baseline)

**Date:** 2026-05-12  
**Incident ID:** `e2e-smoke-e36952331ba9`  
**Alert:** CrashLoopBackOff, same SIGSEGV scenario after OOM text-regex was partially patched.

### Observed

```
facts_observed: ['oom_killed']
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['oom_killed']. Manual triage required."
```

### Problem identified

Even after the structured gate was introduced, the rule evaluation order still allowed the text-regex path to fire first for some pod states. Root cause: the structured gate only blocked when `target_exit` was explicitly non-OOM; it did not block when `k8s_pod_state` was absent.

Also identified: if both `oom_killed` and `process_crash` were observed=True simultaneously, the FactCritic received contradictory evidence and eliminated all hypotheses ("if it OOM-killed it didn't segfault and vice versa").

### Fix shipped

| Item | Change |
|---|---|
| `MUTUALLY_EXCLUSIVE_PAIRS` | `frozenset({oom_killed, process_crash})` — both observed=True is a data contradiction |
| `FactStore.conflicts()` | Enumerates all active conflict pairs |
| `_apply_conflict_signals()` | Caps confidence of conflicting facts to 0.60; sets `evidence.conflict_with` |
| `<conflicts>` prompt block | `FactStore.to_prompt_context()` appends a `<conflicts>` section visible to all agents |

---

## Run 3 — Live SIGSEGV (false positive confirmed)

**Date:** 2026-05-12  
**Incident ID:** `run3-notificator-sigsegv`  
**Alert:** `PodCrashLooping` — `notificator` pod, `squad-10-shared` namespace, exit 139  
**Pod:** real in-cluster pod with 3 restarts

### Observed

```
facts_observed: ['crashloop', 'oom_killed', 'process_crash']
fact_conflicts: [('oom_killed', 'process_crash')]  ← both observed=True
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['crashloop', 'oom_killed', 'process_crash']. Manual triage required."
resolution_quality: unresolved
```

### Root cause of regression

The `OOMKilledRule` text-regex fallback matched the string "OOMKilled" from **other pods'** events in the same namespace. The structured gate had not yet been deployed. The result was:
- `oom_killed`: observed=True (false positive, conf=0.95)
- `process_crash`: observed=True (correct, conf=0.97)

The conflict-cap system fired but the `<conflicts>` signal was not yet wired in the FactCritic context, so the critic eliminated all hypotheses.

### Fix shipped

All items from Runs 1–2 plus:

| Item | Change |
|---|---|
| `OOMKilledRule._check_pod_state()` | Scans all pods in `pod_state`, target-pod first; returns structured Fact on first OOM pod found |
| `target_exit` gate | If target pod has non-OOM exit code (`≠ 0, ≠ 137`) → `return Fact(observed=False)`, never reach text-regex |
| `PERSPECTIVE_PRECONDITIONS` | `{"runtime": {FactKind.PROCESS_CRASH}}` — RuntimeAgent only fires when process_crash is observed |
| Recurrence detection | `SimilarIncidentEngine` detects same-service resolved < 7 days → `recurrence=True` |
| FixAgent recurrence mode | `_RECURRENCE_PREFIX` replaces mitigation with investigative instruction when `is_recurrence=True` |
| Jira enrichment | `JiraClient` queries Atlassian REST API; `build_jira_context()` → `{open, resolved, has_open}` |

---

## Run 4 — Live SIGSEGV (all fixes, Jira integration)

**Date:** 2026-05-12  
**Incident ID:** `notificator-sigsegv-run4`  
**Alert:** `PodCrashLooping` — same `notificator` pod, exit 139  
**Jira:** Atlassian REST API v3 endpoint returned `410 Gone` (deprecated GET /search → graceful degrade)

### Observed

```
facts_observed: ['crashloop', 'process_crash']  ← oom_killed gone!
fact_conflicts: []
hypothesis_survivors: 1
consensus_kinds: ['crashloop', 'process_crash']
cause: "Nil pointer dereference in startup initialization path"
resolution_quality: resolved
is_recurrence: False
jira_context: None  ← graceful degrade (Jira 410)
fix_action: (ExecutionIntent JSON — see Discord approval)
```

### Key improvements demonstrated

| Metric | Run 3 | Run 4 |
|---|---|---|
| `oom_killed` false positive | Yes (conf=0.95) | No |
| `fact_conflicts` | `[oom_killed↔process_crash]` | `[]` |
| Hypothesis survivors | 0 | 1 |
| Cause | "Manual triage required" | "Nil pointer dereference…" |
| resolution_quality | unresolved | **resolved** |
| KG polluted | Yes | No |

### Follow-up action

Jira REST API v3 `GET /search` endpoint returned `410 Gone`. Atlassian migrated to `POST /rest/api/3/search/jql`. `JiraClient` needs to be updated to use the POST endpoint. This is a low-urgency fix — the pipeline degrades gracefully (no Jira context = FixAgent works without it).

---

## Known Limitations / Next Steps

1. **Jira API endpoint**: `GET /rest/api/3/search` is gone; switch to `POST /rest/api/3/search/jql`.
2. **Core dump verification**: `CoreDumpRule` returns a weak signal without real `ls -la /proc/<pid>/coredump_filter` on the host. A `kubectl exec` debug pod with hostPath mount would give exact file sizes and timestamps.
3. **TeamCity correlation**: `teamcity_context=null` in Run 4 — TC MCP was not available in this dev environment. In production, deploy context enriches the root-cause analysis significantly.

---

## Executor incidents

Applies to v0.7.0+ when `EXECUTOR_ENABLED=true` and/or `EXECUTOR_APPROVAL_ENABLED=true` are set. With defaults (both `false`) the pipeline is purely advisory and this section is N/A.

### Recognizing an executor incident

Look at the Discord embed:

| Mode | Embed signals |
|---|---|
| Advisory only (default) | No "Dry-run verdict" field, no Apply button. |
| Executor dry-run only   | "Dry-run verdict" appears (✓/✗/🚫/⚠️). No Apply button. |
| Executor + Apply         | Apply button present iff dry-run passed AND risk ∈ {low, medium} AND `executor_applied` not set. |

DB query:

```sql
SELECT
  incident_id,
  analysis -> 'execution_intent' AS intent,
  analysis -> 'executor_result'  AS dry_run_result,
  analysis -> 'executor_applied' AS applied
FROM incidents
WHERE incident_id = '<id>';
```

| Field | Meaning |
|---|---|
| `execution_intent` | Structured action proposed by `FixAgent` (`NULL` if LLM didn't emit one). |
| `executor_result.status` | dry-run outcome: `skipped` / `dry_run_ok` / `dry_run_failed` / `guardrail_blocked` / `error`. |
| `executor_applied` | Set only after a successful Apply click. `NULL` = no real write happened. |

### Apply failed — what to do

#### kubectl exited non-zero

Symptom: ephemeral shows `❌ kubectl упал: …`, `executor_applied.result.success = false`, `stderr` carries the error.

- `deployments.apps "xxx" not found` — stale name (resource gone between dry-run and apply). No state change. Re-trigger if needed.
- `Operation cannot be fulfilled ... the object has been modified` — concurrent write. Safe to retry.
- `timed out` — kubectl held > 30s. Action may or may not have landed. Verify with `kubectl get deployment <name> -n <ns> -o yaml | grep restartedAt`.

#### Guardrail blocked

Symptom: ephemeral `❌ Не могу применить: …`, audit `EXECUTOR_APPLY_REFUSED` with `reason="dry_run_not_ok:guardrail_blocked"`.

Expected — guard caught a policy violation (e.g., LLM proposed action in `kube-*` or `prod-*` read-only tier). No state change. If the Apply button appeared at all in this state, file a bug.

#### executor_applied present but action was wrong

Revert manually:

```bash
# rollout restart — roll back to previous ReplicaSet:
kubectl rollout undo deployment/<name> -n <ns>

# scale — restore previous count:
kubectl scale deployment/<name> -n <ns> --replicas=<original>
```

Mark the incident as wrong so KG quality gate excludes it:

```sql
UPDATE incidents
   SET analysis = jsonb_set(analysis, '{resolution_quality}', '"wrong_apply"')
 WHERE incident_id = '<id>';
```

Click 👎 on the embed to store user feedback structurally.

### Killing the executor

#### Stop new applies (normal control)

```bash
helm upgrade sre-ai-copilot helm/sre-ai-copilot \
  --reuse-values \
  --set env.EXECUTOR_APPROVAL_ENABLED=false
```

Apply button disappears from new embeds. Already-posted embeds with the button stay in Discord history — clicking them returns `❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.`

#### Emergency (fastest, halts everything)

```bash
kubectl scale deployment/sre-ai-copilot-worker -n sre-ai --replicas=0
```

Halts dry-run, analysis, and apply. Use only if executor is misbehaving badly.

#### Permanent

Set both `EXECUTOR_ENABLED=false` and `EXECUTOR_APPROVAL_ENABLED=false`. Pipeline drops to advisory.

### Audit trail

OTEL root-span attributes:
- `sre.incident.execution_intent_parsed: bool`
- `sre.incident.execution_intent_action`
- `sre.incident.executor_status`

`executor` stage span emits `guardrail.blocked` event if K8sSecurityGuard rejected.

Audit log event types (filter by `event` field):

| Event | When |
|---|---|
| `K8S_GUARDRAIL_BLOCK` | Guard rejected during execute_intent |
| `K8S_COMMAND_ATTEMPT` | About to run kubectl |
| `K8S_COMMAND_RESULT` | kubectl finished |
| `K8S_COMMAND_TIMEOUT` | kubectl > 30s |
| `K8S_BLOCKED_NO_APPROVAL` | `dry_run=False` called without `post_approval=True` |
| `EXECUTOR_APPLIED` | Apply succeeded |
| `EXECUTOR_APPLY_REFUSED` | Apply ineligible |
| `EXECUTOR_APPLY_EXCEPTION` | Unexpected exception in apply-service |
| `EXECUTOR_DRY_RUN_FAILED` | Exception in `stage_executor` |

---

## KG self-health alerts

The `kg_self_health_check` beat task runs six canaries every 30 minutes. On any `fail`/`warn` it posts a single embed to `DISCORD_WEBHOOK_SELF_HEALTH_URL` (this is a separate channel from `#infra-error` on purpose — operational alerts and tooling alerts shouldn't compete).

Audit-log event names: `KG_SELF_HEALTH_OK` / `KG_SELF_HEALTH_WARN` / `KG_SELF_HEALTH_FAIL`. Filter the audit log by `check_name` to drill down.

### `materialization_zero_rate` fail for a specific metric (e.g. `cpu_pct`)

What it means: more than the allowed % of `kg_service_health` rows over the last 24h have value 0 or NULL for that metric.

Allowlist: `http_5xx_rate` and `p95_latency_ms` are still expected to be zero — application `/metrics` endpoints sit behind JWT and return 401 to the scraper; fixing that is a backend change tracked as WO-12483. They should NOT trigger this canary; if they do, the allowlist in `self_health.py` is stale. Until WO-12483 lands, `health_score` is an infra-proxy (no app-level HTTP signal). Note: this no longer applies to ingress-level HTTP metrics — `kg_ingress_observations` is populated (see "Ingress metrics flow" below).

Diagnose in this order:

1. Is VictoriaMetrics reachable? `curl <vm-host>/api/v1/query?query=up`. If not, VM is down — fix VM first; the canary is correctly screaming.
2. Is the PromQL still valid? Open `metrics_sync.py`, paste the query for that metric into the VM UI directly — does it return non-zero on a known-busy service?
3. Did the query shape change recently? See the Wave 5 gotcha: aggregate-then-divide silently returns 0. The correct form is divide-then-aggregate for ratio metrics.
4. Is the celery worker that runs `kg_metrics_sync` healthy? `celery -A app.celery_worker inspect active`.

### `sync_lag` fail for `kg_seq_logs_sync` (or any sync task)

What it means: the latest write timestamp for that source is more than 5× the expected interval old.

Common causes by sync:
- `kg_seq_logs_sync` — Seq instance unreachable, API key expired, or network egress blocked.
- `kg_metrics_sync` — VM unreachable.
- `kg_topology_sync` — kubeconfig invalid or k8s API throttled.
- `tc_deploys_to_kg` — TeamCity MCP unreachable or token expired.
- `kg_ingress_observations_sync` — VM unreachable, the `VMPodScrape` objects `ingress-nginx-shared` / `ingress-prod` (ns `cattle-system`) gone, or per-host metrics disabled on the controller DaemonSet (see "Ingress metrics flow" below).

Generic check: tail the celery worker logs and grep for the task name; look for exceptions or repeated retries.

### Ingress metrics flow (kg_ingress_observations)

Current state (2026-06-10): nginx-ingress metrics are enabled on **both** controllers of the WO cluster (`--enable-metrics=true`; per-host labels must stay on — the sync filters by the `host` label). Scraping goes through `VMPodScrape` objects in ns `cattle-system` with `honorLabels: true`. The beat task `kg_ingress_observations_sync` runs every ~10 minutes and writes per-host/path p95/p99/rps/4xx/5xx rows into `kg_ingress_observations`; 100% of rows are linked to `kg_services`. `error_5xx_rate = 0` with non-zero `rps` now genuinely means "no errors" — not "no data".

If `kg_ingress_observations` stops filling (or goes empty), diagnose in this order:

1. Are the scrape objects alive? `kubectl -n cattle-system get vmpodscrape ingress-nginx-shared ingress-prod`. If either is missing, the metric flow into VM is dead.
2. Is metrics-per-host still enabled on the controller DaemonSets? Disabling it kills the `host` label — and with it the entire sync, even though the controller still exports metrics.
3. Is the flow visible in VictoriaMetrics? Run `count(nginx_ingress_controller_requests) by (namespace)` against `vmsingle-vm-victoria-metrics-k8s-stack.monitoring.svc:8428`. Zero / no series → scrape problem; non-zero → the gap is on the copilot side.
4. If VM has the data but the table doesn't fill — tail the celery worker/beat logs for `kg_ingress_observations_sync` exceptions.

### `anomaly_signal_health: 0 observations`

What it means: the anomaly detector wrote zero rows in the last 24h.

Two distinct interpretations:
- **Flat baseline** — the source metric is all zeros (e.g. `kg_service_health.http_5xx_rate` / `p95_latency_ms`, which stay 0 until WO-12483 lands). Expected; not a problem. Note: ingress metrics no longer qualify — `kg_ingress_observations` has been populated since 2026-06; if the ingress source went flat, check the scrape first (see "Ingress metrics flow" above).
- **Detector regression** — the detector ran but never produced output despite non-zero source data. Inspect `kg_service_health` for variance, then run `anomaly_detection.py` interactively on a known-anomalous service and check the `extras.method` field.

### `anomaly_signal_health: >500 observations`

What it means: thresholds are too loose, or a new metric was added that's inherently noisy.

Tune `KG_ANOMALY_ROBUST_Z_WARN` / `_CRIT` upward, or narrow the baseline window. The volume guard caps at 3/hour per (service, metric), so >500 over 24h implies the count is spread across many services — likely a global threshold issue, not a single hot service.

### `pod_events_link_rate <50%`

What it means: more than half of `kg_pod_events` over the last 24h have `service_id IS NULL`.

This is a StatefulSet resolver regression. Pod naming for StSs uses ordinal suffixes (`-0`, `-1`) instead of the deployment hash; check that `k8s_events_sync.py` still has the cascading fallback from Deployment regex → StatefulSet regex → DaemonSet regex.

### `edges_freshness >30% stale`

What it means: too many `kg_service_edges` have `last_seen_at` older than 24h or NULL.

Most likely cause: `kg_topology_sync` isn't running, isn't refreshing `last_seen_at`, or is upserting under a wrong identity. Check celery beat logs for the hourly run, and verify in DB that the latest `last_seen_at` advances on each run.

---

## Setting up Discord approvers

The Approve / Decline buttons on incident embeds are gated. With no approvers configured the system is fail-closed — buttons are denied with audit `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.

### Get a Discord user ID

In the Discord client, enable Developer Mode (Settings → Advanced → Developer Mode). Then right-click the target user → "Copy ID". The ID is a 17–19 digit numeric string.

### Configure

```bash
# Allowlist by user (comma-separated):
DISCORD_APPROVERS_USER_IDS="123456789012345678,234567890123456789"

# Or by role (allows anyone holding at least one of these roles):
DISCORD_APPROVERS_ROLE_IDS="345678901234567890"

# Optional rate-limit override (default 5/hour per user):
DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR="10"
```

User and role lists are unioned (user-id match OR role match → allowed).

### Fail-closed semantics

If both `DISCORD_APPROVERS_USER_IDS` and `DISCORD_APPROVERS_ROLE_IDS` are empty, every click is denied with `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`. There is no "default allow" — by design.

### Testing

1. Configure the env vars and restart the API + worker.
2. Send a test incident; verify the embed has Approve / Decline buttons.
3. As an allowlisted user: click Approve → ephemeral "approved" reply + audit log line.
4. As a non-allowlisted user: click Approve → ephemeral deny + audit `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`. Verify the deny did NOT consume rate-limit quota for the legitimate approver.

---

## Per-team Discord channels

The Discord renderer can route incidents and digests to different channels per `team_owner`. Configure via `DISCORD_TEAM_CHANNEL_MAP` as a JSON string:

```json
{
  "platform": "https://discord.com/api/webhooks/.../platform-channel",
  "payments": "https://discord.com/api/webhooks/.../payments-channel",
  "gameplay": "https://discord.com/api/webhooks/.../gameplay-channel"
}
```

- Keys match `kg_services.team_owner` (derived from namespace prefix).
- Unmatched teams fall back to `DISCORD_WEBHOOK_URL`.
- Adding a new team: append a new entry; no code change required. Restart the worker to pick up the change.

---

## Signal quality tuning

Anomaly thresholds are tuned via env vars. Robust-z is well-defined statistically (3.5 ≈ p99.95 on a normal-ish distribution), but real metrics have heavy tails.

### Configuration

```bash
KG_ANOMALY_ROBUST_Z_WARN="3.5"   # default — produces "warning" rows
KG_ANOMALY_ROBUST_Z_CRIT="6.0"   # default — produces "critical" rows
```

### Symptoms of false-positive overload

- `anomaly_signal_health` canary turning `warn` with `>500 obs/24h`.
- Discord pipeline starting to dedup itself aggressively (same anomaly hitting every 30 min).
- Operators reporting "noise" in incident embeds.

### How to tune

1. **Raise thresholds** — bump `KG_ANOMALY_ROBUST_Z_WARN` to 4.5 or 5.0. This is the blunt fix.
2. **Narrow the baseline window** — by default the detector uses 7d of history. If a metric has weekly cycles different from daily ones, the seasonal baseline gets noisy. Shortening the window helps if cycles are short.
3. **Check `flat_baseline`** — if many anomalies have `extras.flat_baseline=true`, the metric is genuinely dead and the detector is firing on a "0 → 1" transition. That's usually correct, but it's worth verifying the metric is meant to be alive.

### Symptoms of false-negative

- `anomaly_signal_health` canary turning `warn` with `0 obs/24h`.
- Known incidents not surfacing a deploy correlator signal even though metrics clearly spiked.

Lower thresholds, or check that the seasonal stratification isn't masking the signal (≥50 historical points required — newly-introduced services don't have them yet).

---

## Deploy correlator verdict

Every incident enriched by the pipeline gets a `deploy_correlator` block in `analysis`. The verdict tier drives what the Discord embed shows.

### Verdict meanings

| Verdict | Confidence | Action |
|---|---|---|
| `likely` | ≥ 0.7 | Surfaced as a "Suspect deploy" block in the embed with TC link + author. Investigate the deploy first. |
| `suspect` | 0.4 – 0.7 | Surfaced with softer language ("possibly related"). Investigate after primary hypothesis. |
| `weak` | 0.2 – 0.4 | Kept in `analysis` JSON, not shown in embed. Useful for backfill / audit. |
| `unlikely` | < 0.2 | Recorded for completeness, otherwise ignored. |

### Inspecting confidence directly

```sql
SELECT
  incident_id,
  analysis -> 'deploy_correlator' -> 'verdict'    AS verdict,
  analysis -> 'deploy_correlator' -> 'confidence' AS confidence,
  analysis -> 'deploy_correlator' -> 'factors'    AS factors,
  analysis -> 'deploy_correlator' -> 'deploy'     AS deploy
FROM incidents
WHERE incident_id = '<id>';
```

`factors` shows the multi-factor breakdown:

- `n_spikes` — anomaly count after the deploy in the window
- `max_zscore` — peak robust-z observed
- `time_proximity` — closer deploys score higher
- `deploy_status_factor` — FAILED > SUCCESS
- `flat_baseline_penalty` — dampens confidence on dead-metric "spikes"

### When to dig in manually

- `likely` but the deploy was a docs-only change → check `deploy_status_factor` and `time_proximity`; the deploy might have coincided in time with an unrelated incident. Mark the incident `resolution_quality = 'unresolved'` so it doesn't poison KG.
- `suspect` and you suspect it's actually likely → look at `max_zscore`. If it's just below the warning threshold, the source metric may be borderline noisy.
- `weak` but operator intuition says yes → check whether the deploy was outside the 2h window (incident lag from VM scrape interval can push the timestamp later than reality).

---

## KG quality_report — baseline snapshot

The `quality_report` CLI is the canonical way to take a KG-quality
baseline snapshot — used before/after large remediation waves to
demonstrate change.

### Run it

```bash
# Markdown to stdout (default):
python -m app.scripts.quality_report

# JSON to stdout (for diff / dashboard ingestion):
python -m app.scripts.quality_report --json

# Save snapshot to a file:
python -m app.scripts.quality_report --markdown --output baseline.md
```

The script is **read-only** — no INSERT/UPDATE/DELETE. Safe to run
against production. It uses the same `SessionLocal` as the production
copilot, so it picks up the same DB credentials.

### What it computes

Five sections:

1. **Services** — total / real / synthetic / orphans / by `stale_class`
   (active / expected_stale / suspicious_stale) / owner coverage.
2. **Edges** — by `kind` (calls / uses_nats / uses_db / serves_traffic /
   routes_to / uses_volume / bound_to) / freshness / multi-source ratio.
3. **Events** — deploys by status / pod_events linkage rate (with
   `service_id` resolved) / alerts open vs resolved.
4. **Coverage** — Jobs/CronJobs / Storage Volumes / NATS subjects.
5. **Quality flags** — known data-quality issues with line-number anchors
   (e.g. "12 unowned ns with deploys in last 30d — suspect owner_inference
   gap").

### Baseline at v0.12.0

`docs/quality_report_baseline_2026_05_24.md` — taken right after Wave 8
merge. Use as the comparison anchor for Phase A remediation.

---

## Ownership manifest

The multi-signal owner inference (`ownership_suggester.suggest_owner_multi_signal`)
tries three heuristics in parallel (prefix / deploy-history / labels).
When none fit, or the answer is wrong, override with a YAML manifest.

### Setup

```bash
# Point at a YAML file (must be readable by the worker pod):
OWNERSHIP_MANIFEST_PATH=/etc/sre-ai/ownership.yaml
```

### YAML format

```yaml
# Each entry: ns_pattern (glob), owner (string used as-is in digest),
# reason (free-form, surfaces in audit log). Optional name_pattern lets
# you override a single service inside an otherwise generic ns.
- ns_pattern: "ml-*"
  owner: "@ml-platform"
  reason: "ML infra not yet labeled — owner confirmed via Slack 2026-05-23"

- ns_pattern: "vendor-acme"
  owner: "@vendor-acme"
  reason: "Third-party namespace, no internal owner — escalate via partnership"

- ns_pattern: "*-backup"
  owner: "@platform"
  reason: "All backup CronJobs are platform-owned by policy"

# Per-service override inside a multi-tenant ns:
- ns_pattern: "*-shared"
  name_pattern: "clickhouse*"
  owner: "@data"
  reason: "Analytics stack owned by data team"
```

- `ns_pattern` matches with Python `fnmatch` (glob, not regex).
- `name_pattern` (optional) narrows the rule to a specific service inside
  the ns. Only applied when the suggester is called per-service
  (`suggest_owner_multi_signal(ns, db, name=svc.name)` — which is how
  `app/scripts/backfill_ownership.py` calls it). Digest-level callers
  pass `name=None`, in which case rules with `name_pattern` are skipped
  and only ns-level rules match.
- First match wins — order matters; **place specific rules (with
  `name_pattern`) above generic ns-level catch-alls**.
- A manifest match sets `confidence=1.0` and overrides all three heuristics.

### Bundled manifest for `*-shared` infrastructure

The repo ships `config/ownership.yaml` covering 132 services in
`preprod-shared` / `preupdate-shared` / `prod-shared` / `squad-gd-shared`
that the multi-signal heuristics cannot resolve (no single squad owns
them). Categorization:

- ClickHouse (analytics) → `@data`
- NATS / message bus → `@platform`
- PostgreSQL replicas / backups / metrics → `@platform`
- VictoriaMetrics / kube-state-metrics → `@platform`
- Seq logging, update-service, config-workers → `@platform`
- `squad-gd-shared` app services (auth, push, mv, …) → `@squad-gd`

Mount it via configmap and point `OWNERSHIP_MANIFEST_PATH` at the file.

### Activate `*-shared` ownership manifest in runtime

Master ships the manifest (`config/ownership.yaml`) **and** the Helm
plumbing: `templates/ownership-configmap.yaml` renders a ConfigMap from
`helm/sre-ai-copilot/files/ownership.yaml` (a synced copy — Helm
`.Files.Get` can't reach outside the chart dir), and worker/api
deployments mount it at `OWNERSHIP_MANIFEST_PATH`
(default `/config/ownership.yaml`).

CI gate `tests/test_helm_ownership_sync.py` enforces that
`config/ownership.yaml` ≡ `helm/sre-ai-copilot/files/ownership.yaml`
byte-for-byte. If you edit one, copy to the other:

```bash
cp config/ownership.yaml helm/sre-ai-copilot/files/ownership.yaml
```

#### A. Helm path (recommended)

```bash
# values.yaml ships ownershipManifest.enabled=true by default.
# Edit helm/sre-ai-copilot/files/ownership.yaml if you need an additional
# rule, then:
helm upgrade --install sre-ai-copilot helm/sre-ai-copilot/ \
  --namespace sre-ai \
  -f helm/sre-ai-copilot/values.yaml \
  -f your-overrides.yaml

# Roll worker/api so they pick up the new configmap mount path:
kubectl -n sre-ai rollout restart deploy/sre-ai-copilot-worker
kubectl -n sre-ai rollout restart deploy/sre-ai-copilot-api

# Verify:
kubectl -n sre-ai exec deploy/sre-ai-copilot-worker -- \
  sh -c 'echo $OWNERSHIP_MANIFEST_PATH; head -5 $OWNERSHIP_MANIFEST_PATH'
# Expect: /config/ownership.yaml + first 5 lines of the manifest.
```

#### B. Manual configmap (no Helm)

```bash
kubectl -n sre-ai create configmap sre-ai-copilot-ownership \
  --from-file=ownership.yaml=config/ownership.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# Then patch the deployments to mount /config/ownership.yaml (subPath
# ownership.yaml, readOnly) and set env OWNERSHIP_MANIFEST_PATH=/config/ownership.yaml.
# See templates/deployment-worker.yaml and deployment-api.yaml for the exact shape.
```

#### Backfill already-attributed services

After the configmap is live, re-run inference on `*-shared` so old
attributions converge to manifest verdicts (confidence=1.0 always wins):

```bash
kubectl -n sre-ai exec deploy/sre-ai-copilot-worker -- \
  python -m app.scripts.backfill_ownership --apply --filter-ns '*-shared'
```

The periodic beat task `kg-ownership-backfill` (`OWNERSHIP_BACKFILL_ENABLED=true`)
also re-runs every 6h, but the explicit one-shot above is faster after a
manifest change.

#### Disable

`helm upgrade ... --set ownershipManifest.enabled=false` removes the
ConfigMap, env var, and mount — worker falls back to the three heuristics
(prefix / deploy-history / labels), and 132 `*-shared` services revert
to unowned.

### Adding / changing an override

1. Edit `config/ownership.yaml` — add a new rule. Put specific
   `name_pattern` rules **above** generic `ns_pattern`-only catch-alls.
2. Run `pytest tests/test_shared_ownership_manifest.py -x` locally to
   verify the new rule.
3. Open a PR. Once merged, helm/configmap rollout picks up the change.
4. Re-classify already-attributed services that should now be re-routed:

   ```bash
   kubectl -n sre-ai exec deployment/copilot-worker -- \
     python -m app.scripts.backfill_ownership --apply \
     --filter-ns '*-shared'
   ```

   `--filter-ns` accepts a glob. Manifest matches always apply
   (confidence=1.0 ≥ any threshold).

### Reload

The manifest is re-read on every `suggest_owner_multi_signal` call, but
the file path is cached by environment. To switch manifests, change the
env var and restart the worker.

### Audit

Every manifest match emits a `KG_OWNER_MANUAL_OVERRIDE` audit log line
with `ns_pattern`, `owner`, `reason`. Use to verify manifest is being
read in production.

### Alias map for deploy-history signal

For the deploy-history heuristic (signal B), TC usernames are translated
to team-handles via `app/services/owner_aliases.py`. Override with:

```bash
OWNER_ALIASES_PATH=/etc/sre-ai/owner-aliases.yaml
```

YAML format:

```yaml
kemyashev: "@squad-1"
apleshkov: "@squad-2"
wizaryx:   "@platform"
new-engineer: "@squad-N"
```

Pre-baked defaults in code; YAML extends/overrides. Keys must be lowercase.

---

## Ownership backfill (`app.scripts.backfill_ownership`)

`suggest_owner_multi_signal` is wired into the `unowned_namespaces` digest
section, but **periodic `kg_topology_sync` doesn't call it** — services
that don't get an owner on initial discovery stay `owner=NULL` forever
unless something nudges them. The `backfill_ownership` script closes
that gap: it sweeps every `kg_services` row with `team_owner IS NULL`,
runs multi-signal inference, and writes the result at or above a
configurable confidence threshold.

It also backfills `stale_class` for `stale_class IS NULL` rows via the
same classifier `kg_sync` uses (see `--stale` / `--all`).

### When to run

- **After initial deploy / a fresh restore.** Topology sync only knows
  what k8s labels say; backfill pulls in deploy-history + prefix signals
  that need historical data.
- **When `owner_known < 80%`** in the daily digest or `kg_quality_report`
  (see "KG quality_report" section above). Stagnation usually means
  many services were created before the inference signal existed.
- **Weekly.** Once enabled as a beat task (next section), the periodic
  loop does this automatically; the manual run only matters for the
  first rollout or when you want to lower the threshold for one-off
  catch-up.

### Dry-run preview (default — safe, no writes)

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --dry-run --threshold 0.5
```

Prints `total_candidates_owner`, `would_update_owner`,
`skipped_low_confidence`, and `kept_existing` — without touching the
DB. Use this number to decide whether the apply is worth it.

### Lower threshold to understand the gap

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --dry-run --threshold 0.3
```

Threshold `0.3` surfaces prefix-only suggestions (confidence ≈ 0.4) that
`0.5` rejects. Useful for diagnosing **why** the gap is still there:
if `would_update_owner` jumps from ~50 at `0.5` to ~2000 at `0.3`, the
fix isn't "raise threshold for beat" — it's "configure
`OWNERSHIP_MANIFEST_PATH` so prefix isn't your only signal".

### Apply (production write)

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --apply --threshold 0.4
```

Threshold `0.4` is the recommended initial-rollout setting: includes
prefix matches but not pure single-source `0.3` guesses. Idempotent —
re-running on the same data is a no-op (filtered by
`team_owner IS NULL`).

**Real-world example (2026-05-24):** prod cluster sat at
`owner_known = 12.40%` (335 / 2702 services). One run with
`--apply --threshold 0.4` brought it to `owner_known = 86.68%`
(+74 pp, 1994 services backfilled with `namespace_prefix` as primary
source). The remaining ~13% is genuinely unowned — third-party
namespaces, ad-hoc CronJobs — and is best closed via
`OWNERSHIP_MANIFEST_PATH`.

### Backfill `stale_class` too

```bash
# Only stale_class:
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --stale --apply

# Both ownership + stale_class in one run:
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --all --apply --threshold 0.4
```

### Rollback

The script only ever writes rows where `team_owner WAS NULL` (filter is
in `plan_ownership`). It also stamps `metadata_json.owner_source` with
the signal that won: one of `namespace_prefix`, `k8s_labels`,
`deploy_history` (and `manual` for `OWNERSHIP_MANIFEST_PATH` matches).
To undo a backfill run without touching pre-existing or manually-set
owners:

```sql
UPDATE kg_services
   SET team_owner = NULL
 WHERE metadata_json->>'owner_source'
       IN ('namespace_prefix','deploy_history','k8s_labels');
```

This leaves `owner_source = 'manual'` and the rows where backfill
never wrote (`owner_source IS NULL`, pre-existing owners) untouched.
After the SQL, the next sync will leave services `owner=NULL` until
the next backfill run.

---

## Periodic ownership backfill (beat task `kg-ownership-backfill`)

After the initial manual `--apply` rollout looks healthy, switch the
process from "ops runs it" to "Celery beat runs it" — same `run_backfill`
entry-point, runs every 6 hours, scoped to high-confidence signals only.

### Configuration

| Env var | Default | Notes |
|---|---|---|
| `OWNERSHIP_BACKFILL_ENABLED` | `false` | Master switch. Beat task no-ops when off. |
| `OWNERSHIP_BACKFILL_THRESHOLD` | `0.7` | Confidence floor for periodic writes. |

In Helm, set via `values.yaml`:

```yaml
env:
  ownershipBackfillEnabled: "true"
  ownershipBackfillThreshold: "0.7"
```

Both are wired into the worker Deployment (see
`helm/sre-ai-copilot/templates/deployment-worker.yaml`).

### Threshold rationale: `0.4` initial vs `0.7` periodic

- **Initial rollout (manual, `0.4`):** ops is watching the result, so
  it's fine to include prefix-only matches that need human plausibility
  check. Big one-time win (the +74 pp jump above).
- **Periodic beat (`0.7`):** runs unattended every 6 h. High threshold
  means it only commits multi-signal agreement (e.g. prefix + labels,
  or deploy_history matching prefix). New services that don't clear
  the bar stay `owner=NULL` and surface in the daily `unowned_namespaces`
  digest, where a human can either add `OWNERSHIP_MANIFEST_PATH` entry
  or let the next sync provide enough signal.

Don't lower beat to `0.5` and below "to close the gap" — that defeats
the human-review layer and will repaint owners every 6 h when a
single-signal guess flips. Use a one-off manual `--apply --threshold 0.5`
instead, or extend the ownership manifest.

### Beat schedule + observability

- Schedule: `crontab(minute=17, hour="*/6")` — every 6 h, offset from
  drift/ingress/stuck syncs to avoid DB contention.
- Task name: `kg_ownership_backfill` (Celery flower / logs).
- Log line on each run: `kg_ownership_backfill.done updated=N
  skipped_low_conf=M kept=K`. `updated=0` for many consecutive runs is
  the expected steady state; non-zero only when new services arrive.

---

## stale_class on kg_services

Wave 8 introduces `kg_services.stale_class` (PR #86). Three values:

| Value | Meaning |
|---|---|
| `active` | Deploy within the last `ACTIVE_WINDOW_DAYS` (default 30d). |
| `expected_stale` | Hasn't deployed in 30d, but it's expected: backup/cron/system names, infra/platform-owned namespaces. |
| `suspicious_stale` | No deploys in 30d, doesn't match expected patterns. |

The column is rewritten **idempotently** by `kg_sync.sync_namespace` on
every sync (hourly). The stats_digest reads it as the primary source
with a fallback to the legacy in-memory classifier for installations
that haven't synced yet.

### Reclassifying a service

If a service is misclassified (e.g. an `expected_stale` that actually
needs to deploy more frequently), there are three levers:

1. **Rename**: drop the `-backup` / `-cron` / `-job` suffix that pattern-matches
   into `expected_stale`. The next sync will reclassify it.
2. **Move namespace**: re-deploy into a non-`expected_stale` namespace
   (`kube-system`, `monitoring` are in the system list).
3. **Change owner**: `team_owner = platform` triggers `expected_stale`
   when there are no recent deploys. Set to a squad owner instead.

Manual override via SQL is **not recommended** — the column will be
overwritten on the next sync. Treat it as derived, not authoritative.

### Querying

```sql
-- All suspicious_stale services with their last deploy:
SELECT
  s.namespace, s.name, s.team_owner,
  s.stale_class,
  MAX(d.started_at) AS last_deploy
FROM kg_services s
LEFT JOIN kg_deployments d ON d.service_id = s.id
WHERE s.stale_class = 'suspicious_stale'
  AND NOT s.synthetic
GROUP BY s.id
ORDER BY last_deploy NULLS FIRST;
```

Use this query in production to find candidates for retire/handoff.

---

## Discord snapshot fixtures (UX regression-guard)

Wave 8-G (PR #88) introduces a 7-case snapshot gallery for Discord
embeds — any UX change in `app/services/discord/embed_builder.py` or
related modules has to update these snapshots, or CI fails.

### Cases

`tests/fixtures/discord_snapshots/`:

1. `01_critical_fresh` — first-fire critical alert with full enrichment.
2. `02_critical_resurfaced` — same alert returning after a resolve.
3. `03_warning_compact` — warning severity, no enrichment.
4. `04_burst_aggregation` — same alert firing N times in dedup window.
5. `05_daily_digest` — daily stats digest (KG-summary).
6. `06_chronic_digest` — chronic-suppressed alerts visibility digest.
7. `07_team_digest` — per-team fragile services digest.

Each case has `input.json` (alert/incident payload) and `expected.json`
(rendered embed). The runner is `tests/test_discord_alert_gallery.py`.

### Update workflow

When you intentionally change embed UX:

```bash
# Re-render and overwrite all expected.json files:
UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py

# Review the diff:
git diff tests/fixtures/discord_snapshots/

# Commit if the changes look correct:
git add tests/fixtures/discord_snapshots/
git commit -m "UX: update snapshot fixtures after <change>"
```

### Reviewing snapshot diffs

The runner pretty-prints diffs in pytest output when fixtures fail. Look
for:

- **Title/description text** — the most user-visible regression.
- **Field order** — changes here change scanability of the embed.
- **Color / severity badge** — visual regression at glance.
- **Footer/timestamp** — usually noise; ignore unless ts logic changed.

If a diff is too large to review by eye, run with `-vv` for full
side-by-side, or open `input.json` and re-render in isolation.

### Adding a new case

1. Create `XX_new_case.input.json` with a minimal alert/incident payload.
2. Run `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py -k new_case`.
3. Inspect the generated `XX_new_case.expected.json` — does it look correct?
4. Commit both files together.

Goal: every embed shape (severity × enrichment-state × digest-type)
should have at least one case to detect regression.
