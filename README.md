# SRE AI Copilot

Production-ready Kubernetes incident response AI system.

## Features
- **Webhook Ingestion:** Receives New Relic alerts via FastAPI.
- **Async Processing:** Uses Redis Queue (RQ) for multi-agent analysis.
- **Multi-Agent Pipeline:** 
    - `Analyzer`: Interprets metrics/logs.
    - `Hypothesis`: Brainstorms root causes.
    - `Critic`: Filters/Audits hypotheses.
    - `Fixer`: Suggests `kubectl` commands.
    - `RiskManager`: Evaluates stability impact.
- **Safety Layer:** Dry-run execution and approval gates for Kubernetes.
- **Discord Integration:** Detailed reports with risk assessments.

## Setup

1. **Clone the repo:**
   ```bash
   git clone <repo-url>
   cd sre-ai-copilot
   ```

2. **Configure Environment:**
   Copy `.env.example` to `.env` and fill in:
   - `GEMINI_API_KEY` (Google AI Studio)
   - `DISCORD_WEBHOOK_URL`
   - `REDIS_URL`

3. **Run with Docker Compose:**
   ```bash
   docker-compose up --build
   ```

4. **Expose to New Relic:**
   Use `ngrok` or similar to expose port 8000:
   ```bash
   ngrok http 8000
   ```
   Set New Relic webhook URL to: `https://<your-url>/webhooks/newrelic`

## Safety Model
- `SAFE_MODE=true` ensures no `kubectl` command is run without manual approval.
- Every suggested fix is audited by the `RiskAgent`.
- `K8sService` performs `--dry-run=server` by default.

## Tech Stack
- Python 3.11
- FastAPI
- Redis / RQ
- Gemini Pro API
- Discord.py
- Kubernetes Python Client
