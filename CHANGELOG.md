# Changelog

All notable changes to this project are documented in this file.

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
