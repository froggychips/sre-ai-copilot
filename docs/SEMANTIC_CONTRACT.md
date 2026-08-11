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
