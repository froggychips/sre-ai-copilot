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

Allowlist: `http_5xx_rate` and `p95_latency_ms` are expected to be zero until ingress scrape config is in place — they should NOT trigger this canary. If they do, the allowlist in `self_health.py` is stale.

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

Generic check: tail the celery worker logs and grep for the task name; look for exceptions or repeated retries.

### `anomaly_signal_health: 0 observations`

What it means: the anomaly detector wrote zero rows in the last 24h.

Two distinct interpretations:
- **Flat baseline** — the source metric is all zeros (e.g. ingress metrics with no scrape config). Expected; not a problem.
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
