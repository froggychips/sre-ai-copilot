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

## Where do I report a bug?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) or Telegram [@froggychips](https://t.me/froggychips).
