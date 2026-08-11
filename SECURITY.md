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
- Horizontal access between users (IDOR): reading or writing another user's conversation (`POST /copilot`) or another user's job result (`GET /jobs/{task_id}`)
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
- Anti-replay, three layers, strongest first (`app/security/replay.py`):
  1. **Signed timestamp (preferred).** A signing proxy adds `X-Alertmanager-Timestamp` and signs `ts.body`; the freshness window is `ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS`. `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true` rejects body-only signatures outright. Prometheus AlertManager itself cannot send the header, so this mode requires the proxy.
  2. **Shared nonce store.** A valid signature is claimed once for the whole deployment via Redis `SET NX EX` (TTL = the freshness window), so a captured request cannot be replayed once per replica. Enabled by default in production (`settings.is_production`); overridable with `ALERTMANAGER_REPLAY_SHARED_NONCE=true|false`. Off by default in dev/test so a local Redis cannot make test runs depend on previous runs.
  3. **Per-process seen-signature cache.** Always on, no network. Bounded TTL cache; a repeat inside the window is a 401.
  Layers stack. If Redis is unavailable the store fails **closed with respect to the window**: protection drops back to the per-process cache (exactly the pre-existing behaviour) and never widens the replay window. Degradation is visible in `webhook_replay_shared_store_errors_total`; rejected replays are counted in `webhook_replay_rejected_total{source="local"|"shared"}`.
- Label validation: `alertname` is required; `namespace`, `instance`, `service`, and `app` are checked against conservative character sets before reaching JQL/KG/LLM paths.
- Rate limiting: Redis-backed per-client-IP fixed window. When Redis is unavailable the limit is **not removed** — the check degrades to an in-process window with the same threshold (`app/api/rate_limit.py`), so the cluster-wide ceiling becomes N×limit instead of unlimited. Every degradation increments `rate_limit_redis_errors_total{limiter="alertmanager"}` and `rate_limit_local_fallback_total{limiter,decision}`; the warning log is throttled to once a minute so a Redis outage cannot flood Seq.

Known residual gaps (kept honest):
- Without the signing proxy (layer 1) the HMAC is computed over the body only, so freshness is enforced solely by nonce bookkeeping: a captured request is rejected as a replay, but the *signature itself* never expires. If both the shared store and the process cache have forgotten it (Redis down or key expired, plus a process restart), the same captured body is accepted again. The signed-timestamp mode is the only thing that makes a captured request expire.
- HMAC authenticates the sender, not the alert content — a compromised AlertManager (or anyone holding the secret) can still submit synthetic labels/annotations. Network-level isolation (NetworkPolicy / ingress allowlist) remains a recommended defence-in-depth layer.

### Authentication — JWT bearer tokens

All non-webhook endpoints authenticate with `Authorization: Bearer <JWT>` verified in `app/auth.py`:

- The signature algorithm is pinned to an **asymmetric** allowlist (`RS*`/`PS*`/`ES*`/`EdDSA`). A misconfigured `JWT_ALGORITHM=HS256` would make PyJWT use the *public* key as an HMAC secret; that is a 401, not a fallback.
- `iss` is a required claim on every token, and `aud` is required whenever `JWT_AUDIENCE` is set. In production **both `JWT_ISSUER` and `JWT_AUDIENCE` are mandatory** — enforced at startup by `_enforce_prod_invariants` in `app/config.py`. Without them, a token signed by the same IdP key but issued for a *different* service authenticated here, and its `roles` claim reached `/approvals` and `/replay`.
- Any unexpected validation error is a 401, not a 500 (an empty or malformed `JWT_PUBLIC_KEY` raises `InvalidKeyError`, which is not an `InvalidTokenError`).

**Production is matched by a normalised `ENV`, not a literal.** `settings.is_production` compares `ENV.strip().lower()` against `{"production", "prod"}` and is the single canonical check. Every production invariant (mandatory `JWT_PUBLIC_KEY` / `JWT_ISSUER` / `JWT_AUDIENCE` / `ALERTMANAGER_WEBHOOK_SECRET`, the `SAFE_MODE=false` ban, disabled `/docs`, the wildcard-CORS ban) hangs off it. Point comparisons against the literal `"production"` are how this class of bug shipped twice before: a deployment with `ENV=prod` silently skipped the guards. Note that `staging` is deliberately *not* production for these invariants.

### `GET /jobs/{task_id}` — copilot job results

The endpoint returns the LLM analysis produced for a `/copilot` conversation, so it is an authorization surface, not just a status probe. `task_id` is an unguessable UUID, but it is *not* a secret: it appears in the `Location` header and the body of the `/copilot` response, in ingress access logs, and in application logs. Authentication alone therefore did not stop user A from reading user B's result.

Ownership is now explicit: `/copilot` records `job_owner:<task_id> → <JWT sub>` in Redis (`SET NX EX`, TTL 24 h — the Celery `result_expires` default, so the record never outlives the result it guards), and `/jobs/{task_id}` serves the snapshot only when the stored owner equals the caller's `sub`.

- A foreign task and a non-existent task return the **same 404** — no oracle for enumerating task ids (same rule as `/copilot` and `/approvals/{id}`).
- **Fail-closed:** no owner record (Redis unavailable, expired key, or a task enqueued by a different code path) also means 404. Consequence, stated plainly: `/jobs` serves copilot tasks only, which is what `docs/SEMANTIC_CONTRACT.md` already specifies. For tasks started elsewhere — e.g. `POST /replay/{incident_id}` — poll `GET /webhooks/status/{task_id}`, which returns status without the result payload.
- Redis is the store because the record must be visible to every api replica; an in-process map would break polling as soon as the request lands on a different pod.

### `prompt_guard.py` — telemetry, not a control

`PromptGuard.detect_injection` is seven English-language regexes (`ignore previous instructions`, `dan mode`, `<|endoftext|>`, …). Treat it as **best-effort telemetry, not a security control.** It is bypassed by another language, a paraphrase, an encoding trick, or an indirect injection (text arriving from alert annotations, Seq logs, commit messages, Jira tickets rather than from a human). Do not expand the regex list: extra patterns raise confidence without raising the bar, and a false sense of protection here is worse than no protection.

What actually holds the boundary between "the LLM was talked into something" and "kubectl ran":

- `app/remediation/executor_gate.py::evaluate_intent_gate` — a **deterministic** policy gate that recomputes risk from the `ExecutionIntent` itself, not from the LLM's own `risk` field. A `BLOCK` cannot be argued away in prose.
- `app/services/executor_apply.py` — **server-side namespace binding**: `intent.namespace` must equal the incident's namespace as stored in the database, so a hallucinated or injected intent cannot act in another namespace. Plus a mandatory intent signature (TOCTOU), approval and intent freshness windows, and a fresh `kubectl --dry-run=server` immediately before the write.
- A **mandatory human `APPROVED`** row in `kg_action_approvals`, verified independently of the LLM path.
- `app/security/namespaces.py` and `app/services/k8s_guard.py` — forbidden and read-only tiers; `prod-*` is not writable at all.

Invariant for reviewers: any injection that slips past the regexes (i.e. almost any) must still be stopped by the list above. If `detect_injection` is ever the only thing that stopped an injection, the bug is in the executor gate, not in the regex.

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

- **Alert data reaches Anthropic.** Alert labels and annotations are included in the LLM prompt and sent to the Anthropic API. Do not include PII (usernames, IP addresses, customer data) in alert labels — keep them to service/namespace/metric identifiers. Labels and annotations are **not** redacted — they are treated as operator-authored identifiers.
- **Pod logs are redacted before they reach the LLM.** Container logs collected during fact-gathering (`app/context/k8s_facts.py`, `app/context/logs.py`) go through `app/services/pii_redaction.py::redact_pii` *before* being placed in the facts blob that becomes the prompt, and the same redaction is applied on the Seq→KG write path (`app/knowledge_graph/seq_logs_sync.py`) and again when rendering Discord embeds. Emails, IPs, JWTs, `Bearer`/`Basic` credentials, AWS key ids, UUIDs, PEM private-key blocks (including a block truncated mid-key) and long hex blobs collapse to fixed placeholders; the substitution is idempotent, so double-redaction is a no-op. This is pattern-based, so treat it as a strong default and not a guarantee: a secret in a format no pattern matches still reaches the model. Application logs must not be the place you store secrets.
- **Discord is semi-public.** Approval requests posted to Discord channels are visible to all channel members. Do not include sensitive payload details in Discord messages; use the database record for full context.
- **No credentials in labels.** Never put secrets, tokens, or connection strings in alert labels — they will appear in LLM context, Discord messages, and audit logs.

## Known Limitations

- **Webhook HMAC protects transport, not content.** Signature verification is required by default (fail-closed; explicit `ALERTMANAGER_ALLOW_UNAUTHENTICATED=true` opt-out exists for local dev). Anyone holding the shared secret — including a compromised AlertManager — can still submit synthetic alert payloads. Network-level isolation (K8s NetworkPolicy, ingress IP allowlist) remains a recommended compensating control.
- **Body-only signatures never expire.** With the shared Redis nonce store the per-replica replay window is closed, but a body-only HMAC (what real AlertManager sends) still has no notion of freshness: forget the nonce — Redis outage plus a process restart, or a key that outlived its TTL — and the captured request is accepted again. Only the signed-timestamp mode (`ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true` behind a signing proxy) makes a captured request expire on its own.
- **`prompt_guard` is telemetry, not a control.** The injection regexes are English-only and trivially bypassed; they exist to produce a log signal. The real boundary is the deterministic executor gate, server-side namespace binding, and the mandatory human approval — see the `prompt_guard.py` section above.
- **Namespace guard is pattern-based.** Name-based classification can be fooled by creative naming. A defence-in-depth approach (e.g. namespace labels set by cluster-admin, not the namespace name) would be more robust.
- **LLM output is not formally verified.** The `ExecutionIntent` schema validation catches structural errors but cannot prove the intent matches the operator's goal. Human approval is the last line of defence for destructive actions.
- **Webhook rate limiting degrades, but the ceiling is per-replica.** When Redis is unavailable the limiter falls back to an in-process window with the same threshold, so the effective cluster-wide ceiling is N×limit rather than the intended limit (and it resets on pod restart). A sustained flood from an authenticated source could still exhaust LLM API quota — `LLM_PIPELINE_ENABLED` and Celery rate limits are the backstop.
- **`/jobs` is copilot-only by construction.** Ownership is recorded at enqueue time, so tasks created by other code paths (e.g. `/replay`) have no owner record and are not readable through `/jobs` at all. That is the intended trade-off of the fail-closed check, not an outage.

## Response SLA

| Severity | Example | Target response |
|---|---|---|
| Critical | Prompt injection → unauthorized K8s delete / exec without approval | Patch within 48 h |
| High | Approval endpoint bypass, namespace guard escape | Patch within 7 days |
| Medium | Redis/DB exposure, SAFE_MODE misconfiguration | Patch within 14 days |
| Low | Audit log gaps, Discord information leak | Best effort |
