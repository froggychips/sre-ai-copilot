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

### `POST /webhooks/alertmanager` — unauthenticated ingress

The alertmanager webhook endpoint accepts POST requests without authentication by default. Any party that can reach the endpoint can submit a synthetic alert payload with arbitrary labels and annotations.

Mitigations to consider:
- Deploy behind a network policy or ingress that restricts the source to AlertManager only
- Add HMAC signature verification matching AlertManager's webhook secret support
- Validate that `labels.alertname` and `labels.namespace` match an allowlist before passing to the LLM

Current state: authentication is **not implemented** by default. This is the most critical gap.

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

- **Webhook is unauthenticated by default.** This is the most significant gap. Network-level isolation (K8s NetworkPolicy, ingress IP allowlist) is the primary compensating control until HMAC verification is added.
- **Namespace guard is pattern-based.** Name-based classification can be fooled by creative naming. A defence-in-depth approach (e.g. namespace labels set by cluster-admin, not the namespace name) would be more robust.
- **LLM output is not formally verified.** The `ExecutionIntent` schema validation catches structural errors but cannot prove the intent matches the operator's goal. Human approval is the last line of defence for destructive actions.
- **No rate limiting on the webhook endpoint.** A flood of synthetic alerts could exhaust LLM API quota or create a backlog of pending approvals.

## Response SLA

| Severity | Example | Target response |
|---|---|---|
| Critical | Prompt injection → unauthorized K8s delete / exec without approval | Patch within 48 h |
| High | Approval endpoint bypass, namespace guard escape | Patch within 7 days |
| Medium | Redis/DB exposure, SAFE_MODE misconfiguration | Patch within 14 days |
| Low | Audit log gaps, Discord information leak | Best effort |
