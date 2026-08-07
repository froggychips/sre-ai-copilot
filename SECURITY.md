# Security Policy

## Supported Versions

Security updates are only provided for the current `master` branch. Pinned deployments should track `master` for security fixes.

## Reporting a Vulnerability

**Do not open a public issue.** Please report security vulnerabilities privately:

- **Telegram:** [@froggychips](https://t.me/froggychips)
- **Email:** big@froggychips.xyz

Include: reproduction steps, the payload used (if applicable), Kubernetes version, and what action was triggered or could have been triggered.

## Threat Model

### Primary threat: prompt injection → Kubernetes action

The copilot receives AlertManager webhook payloads and passes alert labels and annotations to an LLM (Anthropic Claude). An attacker who controls an alert's `labels` or `annotations` fields can attempt to inject instructions into the LLM context that cause it to generate an `ExecutionIntent` targeting Kubernetes resources outside the intended scope.

This is the highest-severity threat surface in this project.

### In scope

- Prompt injection via AlertManager alert labels/annotations → unauthorized K8s exec, scale, restart, or delete
- Authentication bypass on the approval endpoint (`POST /approvals/{id}/approve`) allowing an unapproved action to execute
- Namespace guard (`k8s_guard.py`) bypass — a crafted namespace name that passes the pattern check but escapes the intended tier boundary
- `SAFE_MODE=false` in production without compensating controls — this removes the approval requirement for destructive actions
- Redis (Celery broker) compromise leading to arbitrary task injection into the execution queue
- Database (`DATABASE_URL`) exposure leaking approval history or execution logs

### Out of scope

- Compromise of the Anthropic API itself (report to Anthropic)
- AlertManager or Prometheus vulnerabilities (report to those projects)
- Kubernetes API server vulnerabilities (report to the Kubernetes project)
- Attacks requiring existing cluster-admin access
- Discord server compromise (the bot has read/write to a channel, not admin access)

## Sensitive Attack Surfaces

### `POST /webhooks/alertmanager` — HMAC-authenticated ingress

The alertmanager webhook endpoints (`/webhooks/alertmanager`, `/store`, `/enrich-and-forward`) require an HMAC-SHA256 signature (`X-Alertmanager-Signature`, keyed by `ALERTMANAGER_WEBHOOK_SECRET`) and **fail closed**: if no secret is configured, all requests are rejected with 401 in every environment. The only way to run without authentication is the explicit dev opt-out `ALERTMANAGER_ALLOW_UNAUTHENTICATED=true`, which logs a warning on every request. Never set it outside local development.

Additional request validation:
- Anti-replay: a valid signature is accepted once per `ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS` window (in-memory seen-signature cache). Optionally, a signing proxy can add `X-Alertmanager-Timestamp` (HMAC over `ts.body` + freshness window); `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true` makes the timestamp mandatory.
- Label validation: `alertname` is required; `namespace`, `instance`, `service`, and `app` are checked against conservative character sets before reaching JQL/KG/LLM paths.
- Rate limiting: Redis-backed per-client-IP window (fail-open when Redis is down — authentication still applies).

Known residual gaps (kept honest):
- The seen-signature cache is per-process: with N api replicas, a captured request can be replayed once per replica within the freshness window. A shared Redis nonce store would close this fully.
- HMAC authenticates the sender, not the alert content — a compromised AlertManager (or anyone holding the secret) can still submit synthetic labels/annotations. Network-level isolation (NetworkPolicy / ingress allowlist) remains a recommended defence-in-depth layer.

### `k8s_guard.py` — namespace tier enforcement

The guard classifies namespaces into tiers (e.g. `production`, `staging`, `dev`) based on name patterns and enforces which tiers require approval or are forbidden. A namespace whose name matches an unintended pattern could be misclassified.

Review checklist:
- Pattern matching must be anchored (full string match, not substring)
- New namespaces should default to the most restrictive tier, not the least
- `production` tier must always require approval regardless of `SAFE_MODE`

### `ExecutionIntent` DSL and approval flow

The LLM generates structured `ExecutionIntent` objects that are validated before execution. The approval flow (`POST /approvals/{id}/approve`) must verify:
- The approver is an authorized principal (not just any Discord user)
- The intent ID has not already been approved or expired
- The intent was not modified between generation and approval

A replay or TOCTOU attack on the approval endpoint could allow a previously rejected or expired intent to execute.

### `SAFE_MODE=false` in production

When `SAFE_MODE` is disabled, destructive actions (scale-down, restart, delete) execute without requiring approval. This setting must not be active in production without a compensating control (e.g. restricting the webhook source, enabling audit logging, requiring MFA for the approval channel).

### Redis (`REDIS_URL`) — Celery broker

The Celery task queue uses Redis as a broker. If Redis is reachable without authentication or TLS, an attacker could inject arbitrary Celery tasks, including ones that invoke Kubernetes actions outside the normal LLM → intent → approval flow.

Ensure `REDIS_URL` uses authentication (`redis://:password@host`) and is not exposed outside the cluster.

### `DATABASE_URL` — approval and audit log

The database stores execution history and approval records. Exposure of `DATABASE_URL` leaks operational history and could allow tampering with audit logs. Use a least-privilege DB user with read/write only to the application schema.

## Privacy Notes

- **Alert data reaches Anthropic.** Alert labels and annotations are included in the LLM prompt and sent to the Anthropic API. Do not include PII (usernames, IP addresses, customer data) in alert labels — keep them to service/namespace/metric identifiers.
- **Discord is semi-public.** Approval requests posted to Discord channels are visible to all channel members. Do not include sensitive payload details in Discord messages; use the database record for full context.
- **No credentials in labels.** Never put secrets, tokens, or connection strings in alert labels — they will appear in LLM context, Discord messages, and audit logs.

## Known Limitations

- **Webhook HMAC protects transport, not content.** Signature verification is required by default (fail-closed; explicit `ALERTMANAGER_ALLOW_UNAUTHENTICATED=true` opt-out exists for local dev). Anyone holding the shared secret — including a compromised AlertManager — can still submit synthetic alert payloads. Network-level isolation (K8s NetworkPolicy, ingress IP allowlist) remains a recommended compensating control.
- **Replay protection is per-replica.** The seen-signature cache is in-memory per process; a captured request replays at most once per api replica within the freshness window. Use the signed-timestamp mode (`ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true` behind a signing proxy) for stricter guarantees.
- **Namespace guard is pattern-based.** Name-based classification can be fooled by creative naming. A defence-in-depth approach (e.g. namespace labels set by cluster-admin, not the namespace name) would be more robust.
- **LLM output is not formally verified.** The `ExecutionIntent` schema validation catches structural errors but cannot prove the intent matches the operator's goal. Human approval is the last line of defence for destructive actions.
- **Webhook rate limiting is fail-open.** The Redis-backed per-IP limiter passes requests through when Redis is unavailable (authentication still applies). A sustained flood from an authenticated source could still exhaust LLM API quota — `LLM_PIPELINE_ENABLED` and Celery rate limits are the backstop.

## Response SLA

| Severity | Example | Target response |
|---|---|---|
| Critical | Prompt injection → unauthorized K8s delete / exec without approval | Patch within 48 h |
| High | Approval endpoint bypass, namespace guard escape | Patch within 7 days |
| Medium | Redis/DB exposure, SAFE_MODE misconfiguration | Patch within 14 days |
| Low | Audit log gaps, Discord information leak | Best effort |
