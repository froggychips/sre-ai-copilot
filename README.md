# SRE AI Copilot

Production-ready Kubernetes incident response platform powered by multi-agent AI.

## Architecture
- **Async Pipeline:** FastAPI -> Redis (RQ/Celery) -> Workers.
- **AI Engine:** Multi-agent pipeline (Analyzer, Hypothesis, Critic, Fix, Risk) with model routing (cheap/medium/strong).
- **Safety Layer:** K8s DSL Execution, Prompt Guard, and Human-in-the-Loop (HITL) gate.
- **Observability:** OpenTelemetry tracing, Prometheus metrics, structured JSON logs.

## Quick Start
1. Configure `.env` using `.env.example`.
2. Run infrastructure: `docker-compose up -d`.
3. Deploy to K8s: `helm install copilot ./charts/copilot`.

## Safety Model
- **K8s Guardrails:** Strict allow-list for verbs and resources. No destructive commands allowed without approval.
- **Prompt Injection:** XML-based isolation (`<user_context>`) and heuristics blocking code-injection patterns.
- **HITL:** High-risk K8s operations require manual approval via Discord notifications.

## Testing
```bash
pytest tests/
```
Tests mock LLM interactions to verify prompt structure, security guardrails, and error handling logic.

## Disaster Recovery
See `docs/DR.md` for Velero backup procedures and PostgreSQL point-in-time recovery steps.
