# sre-ai-copilot — FAQ

Practical answers for setting up and configuring the AI-powered SRE assistant.

---

## What does this do?

The production path is deterministic, without an LLM. Alertmanager sends alerts to the copilot → it writes them into the knowledge graph (KG) and, for prod/preprod and node-level alerts, posts an enriched card to Discord (owner, recent deploys, blast radius, rule facts). For a few alert classes the remediation bridge (`app/remediation/`) matches a playbook and posts a card with «Apply / Decline» buttons; an operator from the allowlist approves → the action runs in Kubernetes from a separate executor process → the outcome is verified and written back into the same card.

The multi-agent LLM pipeline (Analyzer → Hypothesis → Critic → Fix → Risk, endpoint `POST /webhooks/alertmanager`) is still in the code, but it is **disabled in production** (`LLM_PIPELINE_ENABLED=false`, no route points at it). The same applies to `/copilot` (conversation with the agents) and `/replay` (rerun a past incident without side effects): they exist, but require the LLM backend.

---

## What infrastructure does it need?

| Component | Role |
|---|---|
| Kubernetes | Source of context (pods, deployments, events); target for approved actions |
| Alertmanager | Webhook source (VMAlertmanager in production, `k8s/vmalertmanagerconfig.yaml`) |
| Redis | Celery broker (queues `celery`, `sync`, `executor`) + dedup and approval state |
| PostgreSQL | Knowledge graph, incident records, decisions, audit log |
| Discord bot + webhook | Alert cards, buttons, digests |
| Anthropic API key | Only for the LLM pipeline (`LLM_BACKEND=anthropic`); not needed while it is disabled |

---

## How do I configure it?

Copy `.env.example` to `.env` and fill in the required values:

```env
DATABASE_URL=postgresql+asyncpg://user:pass@host/db
REDIS_URL=redis://:pass@host:6379/0
DISCORD_WEBHOOK_URL=…           # incident embeds
DISCORD_BOT_TOKEN=…             # bot API — required for Apply/Decline buttons
DISCORD_INCIDENT_CHANNEL_ID=…   # channel for incident messages with buttons
DISCORD_APPROVERS_USER_IDS=…    # CSV of Discord user IDs allowed to press the buttons
ANTHROPIC_API_KEY=sk-ant-…      # only if you enable the LLM pipeline
```

Then:
```bash
docker compose up -d
```

The API starts on port 8000. Check `/healthz` for liveness and `/readyz` for readiness (the latter queries PostgreSQL). In production the copilot runs from the manifests in `k8s/` via `deploy.sh` (the Helm chart was removed in 1.0.28).

---

## How does the AlertManager integration work?

There are three receiving endpoints; production routing is in `k8s/vmalertmanagerconfig.yaml`:

| Endpoint | What it does | Who is routed there |
|---|---|---|
| `POST /webhooks/alertmanager/enrich-and-forward` | KG write + enriched Discord card | `prod-*`, `preprod-*`, alerts without a namespace (nodes, control plane) |
| `POST /webhooks/alertmanager/store` | KG write only, nothing in Discord (by design) | `squad-*`, `preupdate-*`, `severity=info` |
| `POST /webhooks/alertmanager` | Full LLM pipeline | nobody (`LLM_PIPELINE_ENABLED=false`) |

Watchdog and `severity=none` go to a blackhole receiver.

The endpoints **fail closed**: they require an HMAC-SHA256 signature (`X-Alertmanager-Signature`, keyed by `ALERTMANAGER_WEBHOOK_SECRET`), and without a configured secret every request is rejected with 401. Stock AlertManager cannot sign the body, so you have two options (details in [SECURITY.md](../SECURITY.md) and `k8s/vmalertmanagerconfig.yaml`):

- put a signing proxy next to AlertManager (preferred; set `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true`);
- or set the explicit opt-out `ALERTMANAGER_ALLOW_UNAUTHENTICATED=true` **and** restrict ingress on port 8000 with a NetworkPolicy to the AlertManager namespace only. The opt-out without that NetworkPolicy leaves the endpoint open to every pod in the cluster.

---

## What happens to each alert?

```
/enrich-and-forward (prod, preprod, nodes)
  → KG: kg_alerts / kg_incidents, attribution to the k8s object (app/services/alert_target.py)
  → dedup (repeat of a still-firing alert is not re-posted)
  → deterministic enrichment (rule facts, owner, deploys, blast radius)
  → Discord card in the incident channel
  → if a playbook may apply: task remediation_bridge in Celery

/store (squads, preupdate, info)
  → KG only; Discord — only a remediation proposal card when an
    applicable playbook with policy «approve» matched

/webhooks/alertmanager (LLM pipeline, disabled in production)
  → IncidentRecord → Celery process_incident → Analyzer → Hypothesis → Critic → Fix → Risk
  → with LLM_PIPELINE_ENABLED=false the task exits without LLM calls
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

In production the buttons come from the deterministic remediation bridge (since 1.0.31; executed in Celery since 1.0.32):

1. An alert whose `alertname` belongs to an executable (v2) playbook enqueues the Celery task `remediation_bridge` (enqueue dedup — Redis `SET NX`).
2. The task collects rule facts + pod state (K8sFacts), `match_playbooks` checks `match` and `preconditions`, the server builds the intent from the playbook's `plan.steps` (not a model), policy decides `approve` / `block`.
3. `approve`: target snapshot (uid), server dry-run, `IncidentRecord(APPROVAL_PENDING)` + `kg_remediation_decisions`, and a card with «Apply / Decline» buttons (`approve:` / `decline:`) in `DISCORD_INCIDENT_CHANNEL_ID`. `block` (prod/preprod/system): no buttons, only a line «playbook X: blocked by policy» in the regular card.
4. An operator from `DISCORD_APPROVERS_USER_IDS` clicks. The click reaches `POST /discord/interactions`; the Ed25519 signature is checked against `DISCORD_PUBLIC_KEY`.
5. With `EXECUTOR_DISPATCH=queue` the write runs in `copilot-executor` (queue `executor`, separate ServiceAccount with write rights) under `K8sSecurityGuard`.
6. Verify checks from the playbook run after the write; the result is appended to the same card.

The bridge needs `REMEDIATION_BRIDGE_ENABLED`, `EXECUTOR_ENABLED` and `EXECUTOR_APPROVAL_ENABLED`; `REMEDIATION_BRIDGE_NAMESPACES` limits it to namespace prefixes (empty = all). In the LLM pipeline (disabled in production) the intent comes from FixAgent and goes through the same gate and button.

---

## Who is allowed to approve?

Set at least one of:
```env
DISCORD_APPROVERS_USER_IDS=111222333,444555666
DISCORD_APPROVERS_ROLE_IDS=123456789
```

Both empty = fail-closed: every Apply/Decline click is denied and audited (`app/api/discord_interactions.py`). The production deployment uses only the user allowlist; roles are not configured. Configure it before the copilot receives real production alerts.

---

## Which namespaces can it write to?

Namespace lists live in `app/security/namespaces.py`; both `K8sSecurityGuard` (`app/services/k8s_guard.py`) and the execution DSL import them:

- `FORBIDDEN_NAMESPACES` — never touched, not even for reads (`kube-system`, `kube-public`, `kube-node-lease`, `chaos-mesh`, `mcp`);
- `READ_ONLY_NAMESPACES` — exact name or `<env>-` prefix for `prod`, `preprod`, `preupdate`, `production`: read-only;
- `WRITE_NAMESPACE_PATTERNS` — the only place writes are allowed (after approval): `^squad-\d+$`, `^squad-gd$`.

Remediation policy additionally classifies the namespace tier (`app/remediation/risk_axes.py`): playbooks allow `approve` only for `dev` / `squad` and `block` `prod` / `preprod` / `system`.

---

## Can I test the pipeline without touching Kubernetes?

There is no global Kubernetes dry-run switch. What exists:

- `EXECUTOR_ENABLED=true` with `EXECUTOR_APPROVAL_ENABLED=false` — the intent is validated by `kubectl … --dry-run=server`, but there is no Apply button, so nothing is written;
- `DISCORD_DRY_RUN=true` — Discord messages are logged instead of sent (local e2e).

---

## What model does it use and can I change it?

Default: `MODEL_NAME` (`claude-sonnet-4-6` unless overridden in `.env`). The multi-step agent pipeline benefits from capable models — smaller models can produce structurally invalid `ExecutionIntent` objects that the DSL validator rejects, causing the action to be skipped silently.

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

`POST /copilot` is a conversation endpoint for asking the incident analysis agents questions directly, without going through AlertManager. It runs up to 3 analysis iterations until confidence reaches the 0.7 threshold. Requires JWT authentication (RS256) — set `JWT_PUBLIC_KEY` in `.env`; in production `JWT_ISSUER` and `JWT_AUDIENCE` are mandatory as well.

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

Playbooks are strict-schema YAML files in `app/remediation/registry/`,
loaded by `load_registry()` in `app/remediation/playbook.py` (`extra="forbid"`
— a typo in any key fails at parse time, not at use; files starting with `_`
are skipped). Contract: [REMEDIATION_PLAYBOOK_CONTRACT.md](REMEDIATION_PLAYBOOK_CONTRACT.md).
Two schema versions:

- **`remediation.playbook/v1`** — preview only (the Phase A layer from
  v0.13.0, PR #99): matched, scored against discrete risk axes, policy
  verdict `auto` / `approve` / `block`, nothing executed. Example below.
- **`remediation.playbook/v2`** — executable: `match.alertnames`,
  `preconditions` on rule facts (`found` / `absent`; `unknown` never
  passes), `plan.steps` from actions known to the executor, `verify` checks.
  Only v2 playbooks are offered by the remediation bridge. In the registry:
  `restart_crashloop_deployment` and `scale_out_cpu_throttled_deployment`.
  The bridge takes alerts of both (`KubePodCrashLooping` / `PodCrashLooping`,
  `CPUThrottlingHigh`; approve only in dev/squad), but builds the plan only
  from server-known parameters: the scale-out plan gets `current_replicas` = live `spec.replicas` and `replicas` = one more (no live replicas or zero — no proposal); `resource_pressure` comes from the firing `CPUThrottlingHigh` itself and `crashloop: absent` only from a successful snapshot where the target pod is not unhealthy (since 1.0.34). `cleanup_stale_failed_job` (v1, `match.classification`
  only) is never taken by the bridge.

`_executor_apply_gate.yaml` is not a playbook: it is the server gate policy
for human-approved applies, loaded only by `executor_gate`.

To add a v1 playbook, drop a new `*.yaml` (unique `name`) with these sections:

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

## Why does a fact show `?` instead of `✓` or `✗`?

Since v1.0.4 a fact has three outcomes. `✓` (`found`) — the rule found it. `✗` (`absent`) — the rule looked in a **live** source and found nothing, which is evidence of absence. `?` (`unknown`) — the rule **could not check**: the source failed, the deploy stream is stale, the service is not in the graph, the alert has no timestamp. Consumers treat `?` as «no evidence either way»: the critic never refutes a hypothesis with it, and Discord shows «❔ Deploy-связь не проверена» instead of staying silent. See `docs/SEMANTIC_CONTRACT.md` §5.1.

## What is an incident in the graph, and why is mine marked `noise`?

`kg_incidents` holds at most one open incident per `(namespace, service)`; alerts of that service attach to it, flapping re-opens it within 30 minutes instead of creating a new one, and it closes when every alert is resolved. `noise=true` means enrichment classified **all** of its alerts as noise — typically `KubeDeploymentGenerationMismatch` with healthy replicas (327 of ~390 weekly alerts on the live cluster), meta-aggregates or a rollout in progress. `GET /kg/incidents` hides those by default (`include_noise=true` shows them); a real alert on the same service clears the flag.

## What do ✓ ◇ ≈ ⌛ ⚠ mean in the «Blast radius» field?

They are epistemic labels from `app/knowledge_graph/epistemic.py`: ✓ observed (endpoints with ready pods, runtime), ◇ declared (read from a k8s manifest — Service, Ingress), ≈ inferred (env variable, secret name, naming convention), ⌛ stale (not confirmed for 7+ days), ⚠ contradicted (sources disagree — e.g. topology says «serves», endpoints say «zero ready pods»). An impacted service inherits the weakest link of its path. Note that every `calls` edge in the graph today is declared or inferred — there is no runtime observation of calls yet, so the field also says «callers may be incomplete».

## Why was my Apply refused with `target_reincarnated`?

Before writing, the executor snapshots the live object and compares its `uid` with the `target_ref` recorded in the graph at analysis time. A different `uid` means the Deployment was deleted and re-created under the same name after the incident — the analysis describes an object that no longer exists. Re-run the analysis on the current object; do not re-approve. If there was nothing to compare with, the write proceeds and `executor_applied.identity_check` says `unknown:*`. After the write a Celery task re-checks the outcome at +5 and +15 minutes (`executor_verification`).

## Where do `KubeJobFailed` alerts land now?

On the Job's owner, not on `vm-kube-state-metrics`. The `service` label of kube alerts is the metric exporter; the target is in `job_name`. Since v1.0.8 `resolve_store_service` looks the Job up in `kg_k8s_jobs` (owner of the Job, then of its CronJob by the `<cronjob>-<unix-minutes>` name pattern), falls back to the CronJob name, and only for ad-hoc Jobs keeps the Job name. Historical rows are moved by `python -m app.scripts.reattribute_job_alerts --apply`.

## Where do I report a bug?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) or Telegram [@froggychips](https://t.me/froggychips).


## Why are there two nodes with the same name in the graph?

Since contract 2.4 a node carries `node_kind`: the k8s Service `auth` and the
Deployment `auth` are **different nodes** (`service` and `workload`), linked by
a `serves_traffic` edge.

Before that they shared one row (keyed by `(namespace, name)`), which meant the
edge between them could not physically exist — it always came out as a
self-loop and was dropped. On the live graph that lost 2092 edges per sync
tick, leaving 3 in the graph.

Practical consequence: looking a node up by `(namespace, name)` is no longer
enough — the query is ambiguous. You need `node_kind`, otherwise
`.one_or_none()` raises `MultipleResultsFound`. The test
`test_no_new_node_lookup_without_node_kind` guards this in code.

## Why didn't orphan_pct drop once serves_traffic appeared?

Because `serves_traffic` is deliberately not counted as connectivity.

That edge links a Service to its own backing workload — a node to its own
implementation. When it appeared at once for every service with a selector,
orphan "improved" from 72.5% to 42.0% while inter-service connectivity did not
change at all: all 1506 "cured" services gained exactly one edge, to their own
workload.

A metric must not improve because the storage schema changed, so
`compute_orphan_stats` excludes `serves_traffic`. Details in `docs/METRICS.md` §6.

## Beat dispatched a task but it never ran — why?

Most likely it expired, and that is intended behaviour.

Every periodic task has a lifetime of ≈90% of its interval. If workers are busy
and the task sat in the queue longer, it is discarded: a `kg_external_probe`
tick dispatched 90 minutes ago answers "how are things right now" and is
useless.

Before `expires` the queue piled up silently — 230 tasks, 94 of them
`kg_external_probe`. Worse, `kg_topology_resources_sync` sat behind that tail
and never ran at all.

Check queue depth with `kubectl -n sre-ai exec redis-0 -- redis-cli LLEN celery`.
Heavy KG syncs (`kg_topology_sync`, `tc_deploys_to_kg`, `kg_storage_sync` and
others, `SYNC_QUEUE_TASKS` in `app/workers/tasks.py`) run in a separate queue
`sync` served by `copilot-worker-sync` (since 1.0.28) — check `LLEN sync` too.
If a queue grows steadily, the workers cannot keep up — a separate problem that
`expires` merely masks.
