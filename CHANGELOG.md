# Changelog

All notable changes to this project are documented in this file.

## [0.7.0] — 2026-05-13

### Added — Executor track

The pipeline can now propose, validate, and (opt-in) execute Kubernetes actions
with human approval. Out-of-the-box behaviour is unchanged (advisory-only) —
both flags default to `false`.

- **Structured `ExecutionIntent`** (`app/agents/fix.py`, `app/core/execution_dsl.py`):
  `FixAgent.suggest()` now returns `(raw_text, Optional[ExecutionIntent])`.
  `ExecutionIntent.from_llm_response()` parses LLM output (plain JSON, code-fence
  wrapper, prose-prefix), validates via pydantic with `FORBIDDEN_NAMESPACES`
  rejected at parse time. Persisted in `record.analysis.execution_intent`.
  10 parser tests + 4 contract tests.
- **Executor stage** (`app/workers/pipeline.py::stage_executor`, between `risk`
  and `synthesize`): when `EXECUTOR_ENABLED=true` runs
  `K8sService.execute_intent(intent, dry_run=True)` →
  `kubectl ... --dry-run=server`. `K8sSecurityGuard.validate` fires first;
  on `GUARDRAIL_BLOCK` → `status=guardrail_blocked`; exception → `status=error`
  with advisory-fallback (pipeline doesn't fail). Result persisted in
  `executor_result`. OTEL attribute `sre.incident.executor_status`. 5 tests.
- **Discord Apply button** (`app/services/discord_service.py`,
  `app/api/discord_interactions.py`): when `EXECUTOR_APPROVAL_ENABLED=true` and
  `dry_run_ok` and `risk ∈ {low, medium}`, the embed gets a `⚙️ Apply (kubectl)`
  button. Two-step confirmation (mirror of 👎 pattern). Discord deferred
  response (type=5) handles >3s kubectl operations via PATCH followup webhook.
  12 tests for the handler + deferred path.
- **`app/services/executor_apply.py`**: canonical apply service. Eligibility
  check (intent present, dry-run ok, risk ≤ medium, not already applied) →
  `k8s_service.execute_intent(dry_run=False, post_approval=True)` → persists
  `executor_applied` with timestamp + `applied_by` (Discord user). Idempotency
  by `incident_id`. 8 tests.
- **`K8sService` restructured** (`app/services/k8s_service.py`): guard validates
  via structural `ActionType → (verb, resource)` mapping (`RESTART_DEPLOYMENT
  → (patch, deployments)`, etc.) instead of brittle kubectl-string parsing.
  Fixes pre-existing latent bug where `cmd_parts[2]="restart"` would fail
  `ALLOWED_RESOURCES` check on every `rollout restart`. `post_approval=True`
  required to bypass `SAFE_MODE` for real writes. 8 tests.
- **Settings**: `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED`,
  `DISCORD_APPLICATION_ID` (for Discord deferred-response followups).
- **Helm chart**: `env.executorEnabled` / `env.executorApprovalEnabled`
  wired into api + worker Deployments.

### Added — Advisory-prod hygiene

- **mypy: 176 → 0 errors, gate is blocking** in CI (`mypy.ini` with per-module
  overrides for SQLAlchemy-ORM `Column[T]` noise + real fixes for `union-attr`
  / `arg-type` in `auth.py`, `discord_service.py`, `llm_service.py`,
  `teamcity_service.py`, `celery_worker.py`, `execution_dsl.py`).
- **ruff: blocking** in CI (`continue-on-error` removed).
- **bandit medium-blocking** preserved; SQL injection f-string in
  `statics_service.py` replaced with `psycopg2.sql.Identifier`; 4 documented
  `# nosec` annotations for false positives.
- **pip-audit blocking** in CI (`protobuf 4.25 → 5.29.6` for CVE-2026-0994,
  `python-dotenv 1.0 → 1.2.2` for CVE-2026-28684; OTEL 1.21 → 1.41.1 to
  satisfy `protobuf<5.0` upper bound).
- **FastAPI lifespan migration**: `@app.on_event` (deprecated since FastAPI
  0.93) → `asynccontextmanager`. Also fixed latent `await engine.dispose()`
  TypeError on a sync `Engine`.
- **`asyncio.run` in Celery task** instead of deprecated `get_event_loop()`.
- **Redis-backed rate limiter** (`app/api/rate_limit.py`): fixed-window 60s
  via Redis INCR+EXPIRE, fail-open on Redis errors. Replaces in-memory
  `defaultdict` that didn't work with multi-replica api.
- **GitHub branch protection** on `master`: required check `Lint and Test`,
  force-push and deletion forbidden, admin can bypass (single-author
  pragmatism).
- **4 pre-existing test failures fixed** (synthesis Russian markers,
  auto_populator fixture, async_integration LLM_BACKEND patch).

### Added — Documentation

- `README.md` (EN + RU): rewritten "Advisory mode" → "Auto-remediator with
  advisory-fallback (off by default)" with ramp-up plan. Roadmap → Execution
  reflects 3/4 done.
- `docs/RUNBOOK.md` (EN + RU): new "Executor incidents" section — how to
  recognize an executor incident, recover from a failed apply, kill the
  executor, audit-trail event types.
- `CHANGELOG.md`: this entry.

## [0.5.0] — 2026-05-12

### Added
- **Fact-anchored reasoning** (`app/diagnostics/`): deterministic rule engine runs before any LLM agent and produces a typed `FactStore` with `FactKind` slugs, confidence scores, and supporting evidence.
- **Multi-hypothesis pipeline** (`app/agents/multi_hypothesis.py`): parallel fan-out to four perspectives (app/infra/deps/runtime); results are adversarially grounded by `FactCriticAgent`.
- **PERSPECTIVE_PRECONDITIONS**: runtime perspective only activates when `process_crash` is observed — prevents noise from unfounded LLM speculation.
- **Fact conflict detection** (`MUTUALLY_EXCLUSIVE_PAIRS`): `{oom_killed, process_crash}` is a contradiction; both observed=True triggers confidence cap at 0.60, `evidence.conflict_with` annotation, and a `<conflicts>` block in the prompt context.
- **OOMKilledRule structured gate** (`app/diagnostics/rules/oom.py`): `_check_pod_state()` scans all pods in `k8s_pod_state`, target pod first; if target exit code ≠ 0 and ≠ 137, returns `observed=False` and skips text-regex fallback — eliminates false positives from other pods' events.
- **KG quality gate** (`_is_quality_cause()`): only high-quality causes are written to the Knowledge Graph; filters out `None`, "No hypothesis survived…", "Manual triage required" strings.
- **Recurrence detection** (`app/core/intelligence/similar_incidents.py`): `RECURRENCE_WINDOW_DAYS=7`; past incidents for the same service that were resolved within the window set `recurrence=True`.
- **FixAgent recurrence mode** (`_RECURRENCE_PREFIX`): when `is_recurrence=True`, FixAgent is instructed to recommend investigative actions (get_logs, describe_resource), not another restart.
- **Jira enrichment** (`app/context/jira_client.py`): `JiraClient` queries Atlassian REST API (Basic Auth); `build_jira_context()` separates open/resolved issues; `_build_jira_prefix()` prepends known issues to FixAgent context.
- **Jira config keys** in `app/config.py`: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `JIRA_BACKEND_LABEL`, `JIRA_SEARCH_DAYS`.
- **analysis fields**: `cause` (None when no survivor), `triage_note`, `resolution_quality`, `fact_conflicts`, `is_recurrence`, `jira_context`.
- **Helm chart** (`helm/sre-ai-copilot/`): full Helm chart with API deployment, Celery worker, Redis StatefulSet, NetworkPolicy, RBAC, PDB, Ingress (nginx + cert-manager + oauth2-proxy).
- **Bilingual documentation** (EN + RU): `docs/RUNBOOK.md`, `docs/RUNBOOK.ru.md`, `docs/ARCHITECTURE.ru.md`, `docs/MODULE_DOCS.ru.md`.

### Changed
- `app/workers/tasks.py`: stage ordering now includes Jira enrichment (stage 4) before FixAgent (stage 5); `is_recurrence` flag wired from SimilarIncidentEngine to FixAgent.
- `app/agents/fix.py`: `suggest()` accepts `is_recurrence` and `jira_context` parameters.
- `docs/ARCHITECTURE.md`: updated to reflect 8-stage pipeline, fact-anchored reasoning, recurrence detection, and Jira integration.
- `docs/MODULE_DOCS.md`: added DiagnosticsEngine, FactStore, JiraClient, SimilarIncidentEngine sections.

### Fixed
- `OOMKilledRule` false positive: text-regex no longer matches OOMKilled events from pods other than the target.
- Knowledge Graph pollution: "Manual triage required" strings no longer stored as resolved causes.
- `SimilarIncidentEngine` data format: `_extract_service_ns()` handles both `targets[0].service` (old) and `labels.service` (new) formats.

### Tests
- `tests/test_diagnostics.py`: 6 conflict detection tests, 4 OOMKilledRule structured gate tests.
- `tests/test_knowledge_graph.py`: 5 KG quality gate tests, 5 recurrence detection tests.
- `tests/test_jira_client.py`: 10 tests covering `_jira_status`, `build_jira_context`, `JiraClient.search_by_service` (mocked httpx).
- `tests/test_multi_hypothesis.py`: 3 perspective precondition tests, updated fan-out tests.

---

## [0.4.0] — 2026-05-10

### Added
- TeamCity MCP integration (`app/services/teamcity_service.py`): enriches incident context with recent deploy data.
- VictoriaMetrics context window (`VICTORIA_METRICS_WINDOW_MINUTES`): memory/CPU metrics before the incident.
- Prompt injection guard (`app/services/prompt_guard.py`): detects injection attempts in external data before it reaches agent prompts.

### Changed
- Context builder now queries TeamCity and VictoriaMetrics in parallel before the agent pipeline.

---

## [0.3.0] — 2026-05-01

### Added
- Human approval flow: `POST /approvals/{incident_id}/approve` and `/reject`.
- Discord webhook integration with dry-run mode (`DISCORD_DRY_RUN`).
- Replay endpoint (`POST /replay/{incident_id}`): re-runs analysis without side effects.
- JWT auth dependency for `/copilot` endpoints.

---

## [0.2.0] — 2026-04-15

### Added
- Celery + Redis async task processing.
- `ExecutionIntent` DSL with `K8sSecurityGuard` policy validation.
- Feedback and evaluation endpoints (`/evaluation`).

---

## [0.1.0] — 2026-04-01

### Added
- Initial release: FastAPI webhook receiver, single-perspective LLM pipeline (Analyzer → Hypothesis → Critic → Fix → Risk), PostgreSQL persistence.
