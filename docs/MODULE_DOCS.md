# Module Documentation

## API Layer
- `app/main.py`: FastAPI init, middleware, routers, health/readiness, async job endpoints.
- `app/api/webhooks.py`: AlertManager webhook endpoint and background task status.
- `app/api/replay.py`: Re-run analysis for a historical `incident_id`.
- `app/api/approvals.py`: approve/reject/get for the approval workflow.
- `app/evaluation/feedback.py`: User feedback ingestion and aggregated stats.

## Workers & Orchestration
- `app/workers/tasks.py`: Celery task `process_incident` — full 8-stage agent pipeline including DiagnosticsEngine, MultiHypothesis, FactCritic, Jira enrichment, Fix, Risk, KG update.
- `app/celery_worker.py`: Celery task `generate_reply`, state transitions, iterative confidence loop.
- `app/core/state_machine.py`: Valid states and lifecycle transitions for an incident.

## Diagnostic Engine
- `app/diagnostics/engine.py`: `DiagnosticsEngine` — evaluates all registered rules against k8s context, applies conflict signals via `_apply_conflict_signals()`, returns a populated `FactStore`.
- `app/diagnostics/facts.py`: `FactKind` (canonical slugs), `Fact`, `FactStore`, `MUTUALLY_EXCLUSIVE_PAIRS`, `FactStore.conflicts()`, `FactStore.to_prompt_context()`.
- `app/diagnostics/rules/oom.py`: `OOMKilledRule` — structured gate first (`_check_pod_state()`), text-regex fallback only when target pod has no exit code data; returns `observed=False` if target exit ≠ 0 and ≠ 137.
- `app/diagnostics/rules/crash.py`: `ProcessCrashRule` — detects SIGSEGV/SIGABRT/non-zero exit codes.
- `app/diagnostics/rules/crashloop.py`: `CrashLoopRule` — detects CrashLoopBackOff state.
- `app/diagnostics/rules/scheduling.py`: `FailedSchedulingRule` — detects resource/taint/affinity scheduling failures.
- `app/diagnostics/rules/deploy.py`: `RecentDeployRule` — correlates TeamCity deploy timing with incident.

## Agents
- `app/agents/base.py`: `BaseAgent` — shared `ask()` method wiring LLM backend.
- `app/agents/analyzer.py`: Primary context/incident analysis.
- `app/agents/multi_hypothesis.py`: `MultiHypothesisOrchestrator` — parallel fan-out to perspectives (app/infra/deps/runtime) filtered by `PERSPECTIVE_PRECONDITIONS`; collects `HypothesisResult` with `survived` flag.
- `app/agents/fact_critic.py`: `FactCriticAgent` — adversarial grounding of each hypothesis against `FactStore`; sets `survived=True/False`.
- `app/agents/fix.py`: `FixAgent` — generates `ExecutionIntent` JSON; `_RECURRENCE_PREFIX` for recurrence mode; `_build_jira_prefix()` for Jira enrichment.
- `app/agents/risk.py`: Risk assessment for the proposed remediation.

## Context & Intelligence
- `app/context/context_builder.py`: Assembles and normalises enriched context from all sources.
- `app/context/logs.py`, `metrics.py`, `deployments.py`: Adapters for k8s logs, VM metrics, deploy history.
- `app/context/jira_client.py`: `JiraClient` (Atlassian REST API v3 Basic Auth); `build_jira_context()` → `{open, resolved, has_open, has_resolved, total}` or `None`.
- `app/core/intelligence/similar_incidents.py`: `SimilarIncidentEngine` — KG search with `_is_quality_cause()` filter, `RECURRENCE_WINDOW_DAYS=7`, `recurrence` flag per result.
- `app/core/intelligence/blast_radius.py`, `temporal_diff.py`, `next_steps.py`: Supporting analysis helpers.

## Data & Persistence
- `app/database.py`, `app/db/*`: Engine/session helpers and database integration.
- `app/models/*` and `app/models.py`: Pydantic/ORM domain models.
- `app/repository.py`: CRUD operations for conversations and messages.

## Services & Safety
- `app/services/mcp_client.py`: Client for executing tools on external MCP servers.
- `app/services/teamcity_service.py`: TeamCity integration for deploy analysis via MCP.
- `app/services/approval_manager.py`: Redis-based approval lifecycle.
- `app/services/k8s_guard.py`: Policy check (verb/resource/namespace/body).
- `app/core/execution_dsl.py`: Strongly typed `ExecutionIntent` and kubectl translator.
- `app/services/resilience.py`: Retry/circuit-breaker logic around LLM calls.
- `app/services/discord_service.py`: Discord notification dispatch.
- `app/services/prompt_guard.py`: Prompt injection detection with input length cap.

## Observability
- `app/telemetry.py`, `app/observability/*`: Tracing, AI metrics, structured logging.
- `app/metrics.py`: Prometheus application metrics.
