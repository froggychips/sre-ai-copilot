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
