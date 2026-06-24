# sre-ai-copilot — FAQ

Practical answers for setting up and configuring the AI-powered SRE assistant.

---

## What does this do?

AlertManager fires a webhook → the copilot receives the alert → a multi-agent LLM pipeline (Analyzer → Hypothesis → Critic → Fix → Risk) produces an analysis and, if needed, an `ExecutionIntent` → the intent goes to Discord for approval → you approve → the action runs in Kubernetes.

The copilot also has a `/copilot` conversation endpoint for asking questions about incidents directly, and a `/replay` endpoint for rerunning past incidents through the agent pipeline without side effects.

---

## What infrastructure does it need?

| Component | Role |
|---|---|
| Kubernetes | Target for actions; source of deployment/pod context |
| AlertManager | Webhook source (kube-prometheus-stack or standalone) |
| Redis | Celery task queue + approval lifecycle state |
| PostgreSQL | Incident records, conversation history, audit log |
| Discord bot | Approval channel notifications and buttons |
| Anthropic API key | LLM inference for all agents |

---

## How do I configure it?

Copy `.env.example` to `.env` and fill in the required values:

```env
ANTHROPIC_API_KEY=sk-ant-…
DATABASE_URL=postgresql+asyncpg://user:pass@host/db
REDIS_URL=redis://:pass@host:6379/0
DISCORD_TOKEN=…
DISCORD_CHANNEL_ID=…        # approval channel ID
DISCORD_APPROVER_ROLE_ID=…  # Discord role allowed to approve
```

Then:
```bash
docker compose up -d
```

The API starts on port 8000. Check `/healthz` for liveness and `/readyz` for readiness (the latter queries PostgreSQL).

---

## How does the AlertManager integration work?

Add a webhook receiver in `alertmanager.yml`:

```yaml
receivers:
  - name: ai-copilot
    webhook_configs:
      - url: 'http://sre-copilot:8000/webhooks/alertmanager'
        send_resolved: true
```

The endpoint has no authentication by default — restrict it at the network level (Kubernetes NetworkPolicy or ingress IP allowlist) so only AlertManager can reach it.

Each alert in the payload generates a separate Celery task and goes through the full agent pipeline independently.

---

## What happens to each alert?

```
Alert arrives at POST /webhooks/alertmanager
  → IncidentRecord created (status: PENDING)
  → Celery task: process_incident
  → Analyzer → Hypothesis → Critic → Fix → Risk agents
  → IncidentRecord updated (status: COMPLETED, analysis attached)
  → Discord report posted
  → If ExecutionIntent generated: approval request posted to Discord
```

---

## Why did an alert show up greyed-out with a 🔇 icon and no @mention?

That is **deliberate render-time suppression** (alert-quality), not a bug
and not a drop. The card is intentionally kept **visible** (so you can
still eyeball it) but muted: grey colour + 🔇, with no 🚨 and no
@mention. Severity is **not** downgraded — only the rendering is toned
down.

There are three reasons a card is muted:

- **`🔇 META-AGGREGATE`** (`meta_noise`, env `META_NOISE_ENABLED`): a
  meta-aggregate `*NewCriticalAlerts` counter, or a derived control-plane
  scrape-gap alert (`etcdInsufficientMembers` / `ScrapePoolHasNoTargets` /
  `RecordingRulesNoData`). Every real critical already arrives as its own
  loud card, so the aggregate adds no signal.
- **`🔇 GENERATION-CHURN`** (`gen_mismatch_noise`, env
  `GEN_MISMATCH_NOISE_ENABLED`): `KubeDeploymentGenerationMismatch` while
  replicas are **healthy** (`ready==desired`). An external controller
  (Rancher) bumps `metadata.generation` as a matter of course; the rollout
  itself converged long ago. **Important:** if replicas are degraded
  (`ready<desired` or unknown) the card stays **loud** (fail-safe loud) —
  a genuinely stuck rollout still rings.
- **rollout window** (`rollout_noise`): the alert arrived inside the
  window of a recent deploy.

How this differs from a fully suppressed (dropped) alert: a dropped alert
(`ALERT_SUPPRESS_NAMES`, Watchdog / InfoInhibitor) never appears in
Discord at all; a muted alert does appear, just quietly.

Each class can be put back into loud mode by setting its env kill-switch
to `false`.

---

## What is SAFE_MODE?

`SAFE_MODE=true` (default): any `ExecutionIntent` that would take a destructive action (restart, scale, delete) requires Discord approval before execution.

`SAFE_MODE=false`: actions execute immediately without approval. Use only in dev/staging environments where speed matters more than the safety check, and only with a restricted webhook source.

Never set `SAFE_MODE=false` in production.

---

## How does the approval flow work?

1. Agent pipeline produces an `ExecutionIntent`
2. Discord message posted with action summary + Approve / Reject buttons
3. Authorized user (configured role or ID allowlist) clicks Approve
4. `POST /approvals/{id}/approve` is called internally
5. Action executes via `ExecutionIntent` DSL → kubectl translator
6. Result posted back to Discord

Intents expire after 30 minutes by default. Expired intents cannot be approved.

---

## Who is allowed to approve?

Set at least one of:
```env
DISCORD_APPROVER_ROLE_ID=123456789
DISCORD_APPROVER_USER_IDS=111222333,444555666
```

Without this, the approval endpoint has no allowlist and any channel member can approve actions. Configure it before the copilot receives real production alerts.

---

## How do I configure namespace tiers in k8s_guard.py?

Edit `app/services/k8s_guard.py`. Namespaces are classified by regex against their name:

```python
TIERS = {
    "production": re.compile(r"^prod.*|.*-prod$"),
    "staging":    re.compile(r"^stag.*|.*-staging$"),
    "dev":        re.compile(r"^dev.*|.*-dev$"),
}
```

- `production` — always requires approval, ignores SAFE_MODE
- `staging` — requires approval when SAFE_MODE=true
- `dev` — executes directly
- Unmatched namespaces default to `production` tier (most restrictive)

Anchor patterns with `^` and `$` to prevent substring matches from misclassifying namespaces.

---

## Can I test the pipeline without touching Kubernetes?

Set `K8S_DRY_RUN=true` in `.env`. The full pipeline runs (AlertManager → agents → Discord approval flow) but actions are logged instead of executed. Good for validating agent output and the approval flow before going live.

---

## What model does it use and can I change it?

Default: whatever is in `ANTHROPIC_MODEL` env var (e.g. `claude-opus-4-7`). The multi-step agent pipeline benefits from capable models — smaller models can produce structurally invalid `ExecutionIntent` objects that the DSL validator rejects, causing the action to be skipped silently.

---

## How do I replay a historical incident?

```http
POST /replay
Content-Type: application/json

{"incident_id": "uuid-of-existing-incident"}
```

Replay reruns the full agent pipeline on the stored incident data without posting Discord notifications or executing Kubernetes actions. Use it to test prompt changes or validate agent behavior on past incidents.

---

## What is the /copilot endpoint?

`POST /copilot` is a conversation endpoint for asking the incident analysis agents questions directly, without going through AlertManager. It runs up to 3 analysis iterations until confidence reaches the 0.7 threshold. Requires JWT authentication — set `JWT_SECRET` in `.env`.

---

## How does the KG figure out who owns a service?

Multi-signal owner inference (`app/services/ownership_suggester.py`), added
in Wave 8 (PR #85). Four signals are tried in parallel:

1. **Prefix** (weight 0.4) — namespace name regex (`squad-N-*` → `@squad-N`,
   `monitoring` / `kube-system` → `@platform`).
2. **Deploy history** (weight 0.4) — most-frequent `triggered_by` over the
   last 30 days of `kg_deployments`. Username → team handle via
   `owner_aliases.py`.
3. **Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
   `app.kubernetes.io/part-of` in `kg_services.metadata_json`.
4. **Manual override** — `OWNERSHIP_MANIFEST_PATH=ownership.yaml` glob
   match → confidence=1.0, overrides everything.

Top-1 candidate by summed `weight × signal_strength` wins. See
[Ownership manifest in RUNBOOK](RUNBOOK.md#ownership-manifest) for how
to add overrides.

---

## Why does the daily digest say "(new baseline)"?

The daily stats digest shows trend Δ24h for every metric (alert count,
deploy success rate, fragile services count, …). The trend needs a
yesterday-state row to compare against.

When the digest runs for the first time, or the yesterday-state row was
purged, there's no comparison anchor — so we print `(new baseline)`
instead of a fake `Δ +0`. Wave 8-F (PR #90) added this explicit
placeholder; before it, the trend silently showed 0 which was misleading.

The placeholder will disappear after the next digest run (24h later)
once a yesterday-state exists. If you see `(new baseline)` more than
once in a row, the state-persistence is broken — check
`kg_stats_digest_state` rows.

---

## What does `stale_class` on a service mean?

Wave 8 (PR #86) added `kg_services.stale_class` with three values:

- **`active`** — deploy within the last 30 days. Normal operating state.
- **`expected_stale`** — hasn't deployed in 30d, but it's expected:
  backup/cron/system patterns (`*-backup`, `*-cron`, `kube-system`,
  `monitoring`) or `infra`/`platform`-owned namespaces.
- **`suspicious_stale`** — no deploys in 30d, doesn't match expected
  patterns. Candidate for retire/handoff investigation.

The column is rewritten idempotently by `kg_sync.sync_namespace` on
every hourly sync. Stats digest hides `expected_stale` by default via
`STATS_HIDE_EXPECTED_STALE=true` to keep the noise down. See
[stale_class in RUNBOOK](RUNBOOK.md#stale_class-on-kg_services) for
how to reclassify a misclassified service.

---

## Why are `http_5xx_rate` / `p95_latency_ms` in kg_service_health always 0?

Because application `/metrics` endpoints sit behind JWT and return 401
to the scraper. Fixing that requires a backend change — tracked as
WO-12483. Until it lands, `health_score` is an infra-proxy
(no app-level HTTP signal).

This does **not** mean there is no HTTP signal at all: ingress-level
metrics are live (as of 2026-06-10). nginx-ingress metrics are enabled
on both controllers of the WO cluster (`--enable-metrics=true`, per-host
labels kept on), scraped via `VMPodScrape` in ns `cattle-system` with
`honorLabels: true`, and the `kg_ingress_observations_sync` beat task
writes per-host/path p95/p99/rps/4xx/5xx rows into
`kg_ingress_observations` every ~10 minutes — 100% of rows linked to
`kg_services`. An `error_5xx_rate = 0` with non-zero `rps` genuinely
means "no errors", not "no data".

See [Ingress metrics flow in RUNBOOK](RUNBOOK.md#ingress-metrics-flow-kg_ingress_observations)
for what to check when the table stops filling.

---

## How do I add a new playbook (Phase A remediation)?

**Phase A foundation is implemented as of v0.13.0 (PR #99): decision
*preview* without an executor.** Playbooks are matched, scored against 8
discrete risk axes, and run through a rule-based policy evaluator that
produces an `auto` / `approve` / `block` verdict — but the plan is **not
executed**; Phase A only surfaces the recommended action. (Actual writes
remain the separate, opt-in per-incident `executor` track gated behind
`EXECUTOR_APPROVAL_ENABLED` — it is not yet driven by playbooks.)

A playbook is a single strict-schema YAML file in
`app/remediation/registry/`, loaded by `load_registry()` in
`app/remediation/playbook.py`. Schema is `remediation.playbook/v1` with
`extra="forbid"` — a typo in any key fails at parse time, not at use.
To add one, drop a new `*.yaml` (unique `name`) with these sections:

```yaml
schema_version: remediation.playbook/v1
name: cleanup_stale_failed_job          # unique; one playbook per file
kind: remediation
description: |
  What this does and when it is safe.
match:                                   # when this playbook applies
  classification: stale_failed_job
  job_age_hours: {gte: 24}               # numeric constraints: gte/lte/gt/lt/eq
  active_jobs: {eq: 0}
policy:                                  # verdict by context
  auto:                                  # eligible for auto (when executor lands)
    namespace_tier: ["dev", "squad"]
    owner_kind: "CronJob"
  approve:                               # human approval required
    namespace_tier: ["dev", "squad"]
    owner_kind: ["None", "helm_hook"]
  block:                                 # never act
    any:
      namespace_tier: ["prod", "preprod", "system"]
plan:                                    # templated command + read-only preview
  command: ["kubectl", "delete", "job", "{job_name}", "-n", "{namespace}"]
  preview: ["kubectl", "get", "job", "{job_name}", "-n", "{namespace}", "-o", "yaml"]
observe:                                 # success / failure signals
  timeout: 5m
  success: {job_exists: false}
  failure: {job_exists: true, new_job_failed: true}
```

See `app/remediation/registry/cleanup_stale_failed_job.yaml` for the
canonical example, and [Roadmap → Execution](../README.md#roadmap--execution)
for how the executor track (per-incident `kubectl --dry-run=server` +
Discord Apply, PR #23/#26/#27) relates to this preview layer.

---

## How do I take a KG quality snapshot?

```bash
python -m app.scripts.quality_report --markdown --output baseline.md
```

The `quality_report` CLI (PR #87) is read-only — safe to run against
production. It computes 5 sections (services, edges, events, coverage,
quality flags) and outputs markdown or JSON.

Use it before/after large remediation waves to demonstrate measurable
change. Baseline at v0.12.0: `docs/quality_report_baseline_2026_05_24.md`.

---

## Where do I report a bug?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) or Telegram [@froggychips](https://t.me/froggychips).
