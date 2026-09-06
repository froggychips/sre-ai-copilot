# Semantic Contract

This document describes the actual contract of the current implementation (v0.5.x) to synchronise the API, workers, and operations.

## 1. Incident Lifecycle Contract

`IncidentRecord.status`:
- `PENDING` — incident accepted by the API, queued.
- `COMPLETED` — pipeline completed successfully, results saved.
- `FAILED` — error during analysis/generation.

`analysis` JSON fields (set on `COMPLETED`):
- `cause: str | null` — root cause string, or `null` when no hypothesis survived.
- `triage_note: str | null` — populated when `cause` is null; describes the no-survivor reason.
- `resolution_quality: "resolved" | "unresolved"` — quality gate result.
- `fact_conflicts: list[str]` — list of conflicting fact-kind pairs, empty if none.
- `is_recurrence: bool` — true if the same service had a resolved incident within 7 days.
- `jira_context: dict | null` — Jira enrichment result `{open, resolved, has_open, has_resolved, total}`, or null if Jira is disabled or failed.
- `fix: str | dict` — `ExecutionIntent` JSON string or dict from FixAgent.
- `hypotheses: str` — formatted multi-hypothesis output with `[SURVIVED]`/`[REJECTED]` tags.
- `summary: str` — human-readable summary from SynthesisAgent.

For the conversational pipeline, an additional state machine (`IncidentState`) is used with transitions: `INVESTIGATING → HYPOTHESIS_GENERATED → FIX_PROPOSED`.

## 2. Async Job Contract

For endpoints that create background tasks, the response contains:
- `task_id` — Celery task identifier.
- `status` or `location` — way to check progress.

Consumers must poll:
- `/webhooks/status/{task_id}` for the webhook pipeline.
- `/jobs/{task_id}` for the `/copilot` pipeline.

## 3. Feedback Contract

`POST /evaluation/{incident_id}/submit` accepts:
- `score: int`
- `is_accepted: bool`
- `comment?: str`

`GET /evaluation/stats` returns:
- `total_evaluated_incidents`
- `accepted_count`
- `accuracy_rate`

## 4. Execution Safety Contract

Every remediation operation must satisfy two layers:
1. **DSL contract** (`ExecutionIntent`):
   - `action` from enum (`restart_deployment`, `scale_deployment`, `get_logs`, `describe_resource`)
   - `resource_type` constrained by regex `(deployment|pod|service|ingress)`
   - system namespaces blocked by the validator
2. **Runtime policy** (`K8sSecurityGuard`):
   - verb/resource allowlist
   - namespace blocklist
   - deep body inspection (e.g. `privileged: true` is rejected)

## 5. FactStore Contract

`FactStore` is the ground-truth about an incident. Rules are the only writers; agents are read-only consumers.

`Fact` fields:
- `kind: FactKind` — canonical slug (see `FactKind` enum).
- `observed: bool` — whether the fact was confirmed by the rule.
- `confidence: float` — 0.0–1.0; automatically capped to 0.60 when in a conflict pair.
- `evidence: dict` — supporting data; `conflict_with` key added when in a conflict.

`FactStore.to_prompt_context()` serialises facts as a `<facts>` XML block and appends `<conflicts>` when `conflicts()` is non-empty. Agents MUST NOT modify this; they only read it.

### 5.1 Evidence Contract (verdict, epistemic, provenance)

A fact has **three** outcomes, not two. `observed` alone cannot express the third:

| `verdict` | meaning | `observed` | `confidence` |
|---|---|---|---|
| `found` | the rule looked and found it | `True` | rule-defined |
| `absent` | the rule looked in a **live** source and found nothing — evidence of absence | `False` | rule-defined (high) |
| `unknown` | the rule **could not check**: source failed, stale, service not in KG, no timestamp — absence of evidence | `False` | forced `0.0` |

Rules:
1. `Fact.unknown(kind, reason, ...)` is the only way to produce `unknown`; `unknown_reason` is mandatory and human-readable.
2. Consumers MUST distinguish `absent` from `unknown`. `absent` may refute a hypothesis; `unknown` MUST NOT (`fact_critic._algorithmic_refutations` skips unknown anchors; the LLM critic prompt states the same). `to_prompt_context()` renders `✓` / `✗` / `?`.
3. Every `Rule` declares `sources: tuple[str, ...]` — the ctx fields its answer depends on. `Rule.run(ctx)` (the engine and `alert_enrichment` entry point) demotes `absent` → `unknown` when any declared source is listed in `ctx["source_status"]`. `found` is never demoted.
4. `ctx["source_status"]: dict[field → reason]` is filled by whoever builds the ctx (`alert_enrichment.enrich_alert`: query exception, `deploy_stream_freshness().stale`, service not in KG). A field absent from the dict is considered successfully queried: an empty list then means *empty*, not *unknown*.
5. Optional evidence metadata: `epistemic` (`app.knowledge_graph.epistemic.Epistemic` value — observed / declared / inferred / …), `provenance` (data source, e.g. `kg_deployments/service`, `k8s_event`), `window_min` (search window; `absent` without a window is meaningless), `data_age_min`.
6. Deployment attribution: `recent_deploys_for*` return `attribution_scope` ∈ `service` (exact record, `namespace_scope=False`) / `namespace` (ns-broadcast) / `unknown` (legacy). `RecentDeployRule` emits `epistemic=observed, confidence=0.95` only for `service`; otherwise `inferred, 0.6` with an explicit note. Discord renders the inferred case as "в namespace — привязка к сервису не подтверждена" and an `unknown` deploy fact as the field «❔ Deploy-связь не проверена».

Guard: `tests/test_evidence_contract.py` — every rule in `DEFAULT_RULES` declares `sources`, and with all of them marked failed no rule emits an `absent` fact.

## 6. KG Quality Gate Contract

`_is_quality_cause(cause, resolution_quality)` returns `False` for:
- `cause` is `None`
- `resolution_quality` is not `"resolved"`
- `cause` starts with "No hypothesis survived…" or "Manual triage required"

Only incidents passing this gate enter the Knowledge Graph as candidates for `SimilarIncidentEngine`.

## 7. Recurrence Detection Contract

`SimilarIncidentEngine.find_similar()` returns `recurrence=True` for a result when:
- The matched incident's service equals the current service
- `resolved_at >= now() - RECURRENCE_WINDOW_DAYS` (default: 7 days)
- The matched incident has `resolution_quality = "resolved"` (passed quality gate)

When `is_recurrence=True` in the pipeline, `FixAgent` MUST use `_RECURRENCE_PREFIX` — it MUST NOT recommend a simple restart.

## 8. External Data Sanitisation Contract

Data received from external systems (e.g. commit messages from TeamCity, Jira issue summaries) MUST be sanitised before inclusion in agent prompts to prevent prompt injection attacks.

## 9. Replay Mode Contract

A replay scenario re-runs analysis of a historical incident and MUST:
- not trigger external side effects (e.g. Discord alerts),
- allow flexible state-machine transitions for retrospective runs.

## 10. Time / Timezone Contract

Every timestamp persisted by this service is **naive UTC**, and the database
session is **pinned to UTC**. Both halves are load-bearing — one without the
other silently shifts every time window.

1. **Storage.** All `DateTime` columns are `TIMESTAMP WITHOUT TIME ZONE`
   (`timezone=True` MUST NOT be used). Python-side defaults are
   `datetime.utcnow()` — naive, valued in UTC. The column carries no offset,
   so "which zone is this" is known only to the code.
2. **Session.** `app/database.py` passes `-c timezone=UTC` in `connect_args`
   for the Postgres path. This is what makes (1) self-consistent: dozens of
   raw-SQL windows compare a naive column against `NOW()`, which is
   `timestamptz` — e.g. `WHERE ts > NOW() - INTERVAL '24 hours'`
   (`app/services/stats_digest.py`) and `SET resolved_at = NOW()`
   (`app/knowledge_graph/alerts_resolve_sync.py`). Postgres coerces
   `timestamptz` to `timestamp` **using the session TimeZone**. At UTC the
   result matches what `utcnow()` wrote; at any other zone — and the server
   default comes from outside the application (`postgresql.conf`,
   `ALTER ROLE`, the pod's `PGTZ`) — every window slides by the offset. The
   daily digest reports the wrong 24 hours, `alerts_resolve_sync` under-resolves
   alerts, and nothing errors or alerts.
3. **Boundaries.** Naive and aware datetimes MUST NOT be mixed: comparing them
   in Python raises `TypeError`, and writing an aware value into a column
   without a zone drops `tzinfo`, shifting the value. Code at the edge
   (API/JSON payloads, external clients) converts explicitly before persisting:
   `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

Guards: `tests/test_db_engine_pool_config.py` (session TimeZone pinned, model
defaults naive UTC, no `timezone=True` column anywhere) and
`tests/test_idle_transaction_guard.py` (the option survives on the real engine).

> Note: the codebase still mixes `datetime.utcnow()` and
> `datetime.now(timezone.utc)` at call sites. That is tolerable **only** under
> this contract — values written to the DB must end up naive UTC. A migration
> to aware-everywhere would have to flip the columns to `timestamptz` in the
> same change; doing one half alone is a silent-offset bug.

## 11. Incident Contract (`kg_incidents`)

An **Incident** is a deterministic object built from a service's alerts — no LLM, no "roughly at the same time" heuristics. It exists because `kg_alerts.incident_id` used to equal `fingerprint` in every row, i.e. "incident" was another name for one alert.

1. **Identity.** At most one `open` incident per `(namespace, service_name)`; enforced by the partial unique index `uq_kg_incidents_one_open_per_service`, not by application code. `incident_key = "<ns>/<service>@<opened_at:%Y%m%dT%H%M%S>"` is stable for the life of the row; the integer `id` is used in URLs.
2. **Attach** (`incidents.attach_alert`, called from `auto_populator.populate_from_incident` after `record_alert_event`): a firing alert joins the open incident of its service; if none is open and one was resolved less than `REOPEN_WINDOW_MIN` (30) minutes before `fired_at`, that one is **reopened** (`reopened_count += 1`); otherwise a new incident is opened. Idempotent by `fingerprint`. `kg_alerts.incident_id` is set to the `incident_key`.
3. **Aggregates.** `alert_count` = distinct fingerprints; `severity` = max over alerts (critical > warning > info); `opened_at` = min `fired_at` (a late alert widens the window but never changes the key); `last_alert_at` = max `fired_at`.
4. **Resolve** (`incidents.reconcile_incidents`, beat task `kg_incidents_lifecycle` every 5 min): all linked alerts have `resolved_at` → `status=resolved`, `resolved_at = max(alert.resolved_at)`, `resolve_reason=all_alerts_resolved`. No new alert for `AGE_OUT_HOURS` (168) and still open → `resolve_reason=aged_out`. `resolved` is terminal except for reopening (rule 2).
5. **Timeline** (`incident_timeline.build_timeline`, `GET /kg/incidents/{id}/timeline`): events from `kg_incidents`, `kg_alerts`, `kg_deployments`, `kg_pod_events`, `kg_anomaly_observations` (bucketed per metric·hour), `kg_log_observations` (error-level only) in `[opened_at − 60 min, (resolved_at | now) + 30 min]`, sorted by `ts` then causal kind order. Every event carries `evidence = {epistemic, provenance}` (§5.1): deploys are `observed` only for `attribution_scope=service`, otherwise `inferred`; anomalies are always `inferred`. `unknowns` lists sources that could **not** be queried (e.g. `service_id` is NULL) — an empty timeline is never silently equal to "nothing happened".
6. **Cross-service correlation is out of scope here.** Linking incidents of different services (shared dependency, deploy in window, `calls` edge) is a later roadmap step and will be expressed as edges between incidents, never by relaxing rule 1.
