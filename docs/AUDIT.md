# SRE AI Copilot — Audit Trail via OpenTelemetry

This document describes the OTEL instrumentation added in v0.5.x for a complete, traceable audit trail of all AI-agent actions.

---

## Why audit matters here

The copilot can propose and (after human approval) execute Kubernetes operations. In an incident post-mortem or compliance review you need to answer:
- **Who/what** triggered the analysis? (AlertManager webhook, MCP tool call, replay)
- **What did the AI decide?** (root cause, recommended action, risk level)
- **Did guardrails fire?** Which rules blocked what?
- **Was approval obtained?** Who approved, when, for what?
- **What kubectl command was generated?**

All of this is captured as a single distributed trace per incident.

---

## Span tree (one incident)

```
sre.copilot.incident.process          ← root span (tasks.py)
  │  sre.incident.id = "WO-1234-abc"
  │  sre.incident.service = "notificator"
  │  sre.incident.namespace = "squad-10-shared"
  │  sre.user.source = "alertmanager"
  │  sre.incident.resolution_quality = "resolved"
  │  sre.incident.is_recurrence = false
  │
  ├─ sre.copilot.agent.analyzer
  ├─ sre.copilot.agent.fixer               ← @trace_agent("Fixer")
  ├─ sre.copilot.agent.risk
  │
  ├─ sre.copilot.execution.intent          ← DSLTranslator.to_kubectl()
  │    sre.execution.intent.action = "restart_deployment"
  │    sre.execution.intent.resource_name = "notificator"
  │    sre.execution.intent.namespace = "squad-10-shared"
  │    sre.risk.level = "medium"
  │    sre.execution.intent.dsl = "{...full JSON...}"
  │
  ├─ sre.copilot.approval.request          ← ApprovalManager.request_approval()
  │    sre.approval.id = "uuid-..."
  │    sre.approval.user_id = "webhook"
  │    sre.risk.level = "medium"
  │
  └─ sre.copilot.approval.approved         ← ApprovalManager._atomic_transition()
       sre.approval.id = "uuid-..."
       sre.approval.status = "APPROVED"
```

When a guardrail fires, a `guardrail.blocked` **event** is emitted on the closest enclosing span:

```
span.events:
  - name: "guardrail.blocked"
    attributes:
      sre.guardrail.reason: "write_to_readonly_ns"
      sre.guardrail.decision: "blocked"
      sre.namespace: "prod"
      sre.verb: "patch"
```

---

## Semantic conventions (sre.copilot.*)

| Attribute | Type | Description |
|---|---|---|
| `sre.incident.id` | string | Incident ID (fingerprint from AlertManager) |
| `sre.incident.service` | string | Service label from the alert |
| `sre.incident.namespace` | string | Kubernetes namespace |
| `sre.user.source` | string | `alertmanager` / `mcp.claude` / `replay` |
| `sre.incident.resolution_quality` | string | `resolved` / `unresolved` |
| `sre.incident.is_recurrence` | bool | True if same service resolved < 7 days ago |
| `sre.incident.cause` | string | Root cause (first 500 chars) |
| `sre.agent.name` | string | Agent name (Fixer, Risk, Analyzer, …) |
| `sre.execution.intent.action` | string | `restart_deployment`, `get_logs`, … |
| `sre.execution.intent.resource_type` | string | `deployment` / `pod` |
| `sre.execution.intent.resource_name` | string | Resource being acted on |
| `sre.execution.intent.namespace` | string | Target namespace |
| `sre.execution.intent.dsl` | string | Full `ExecutionIntent` JSON (truncated to 2000 chars) |
| `sre.risk.level` | string | `low` / `medium` / `high` |
| `sre.approval.id` | string | UUID of the approval request |
| `sre.approval.action` | string | `request` / `approved` / `rejected` |
| `sre.approval.user_id` | string | Who created the request |
| `sre.approval.status` | string | Final status after transition |
| `sre.guardrail.decision` | string | `allowed` / `blocked` |
| `sre.guardrail.reason` | string | Why it was blocked |
| `sre.namespace` | string | Namespace in guardrail context |
| `sre.verb` | string | Kubernetes verb in guardrail context |

---

## Where spans are created

| Span name | File | Method |
|---|---|---|
| `sre.copilot.incident.process` | `app/workers/tasks.py` | `async_process_incident()` |
| `sre.copilot.agent.*` | `app/services/telemetry_utils.py` | `@trace_agent` decorator |
| `sre.copilot.execution.intent` | `app/core/execution_dsl.py` | `DSLTranslator.to_kubectl()` |
| `sre.copilot.approval.request` | `app/services/approval_manager.py` | `request_approval()` |
| `sre.copilot.approval.<status>` | `app/services/approval_manager.py` | `_atomic_transition()` |
| `guardrail.blocked` (event) | `app/services/k8s_guard.py` | `K8sSecurityGuard.validate()` |

---

## Export & storage

**Development**: traces go to Tempo (if `OTLP_EXPORTER_ENDPOINT` is reachable) or are silently dropped (no-op exporter). No data loss in the pipeline.

**Production recommendation**:
1. OTEL Collector → Tempo (traces, 30-day retention)
2. structlog audit log → Loki (already implemented, stdout)
3. Critical spans (`sre.risk.level=high`, `sre.guardrail.decision=blocked`) → 100% tail-based sampling

**Helm**: `values.yaml` exposes `env.otlpExporterEndpoint`. Point it at your OTEL Collector ClusterIP.

---

## Next: MCP instrumentation (planned)

When the copilot MCP server is implemented, each tool call will emit a `sre.copilot.mcp.tool_call` child span with:
- `sre.user.source = "mcp.claude"`
- `mcp.tool_name` = the tool being called
- `mcp.trace_id` = W3C traceparent from the MCP request headers

This ties the full Claude ↔ Copilot dialog into a single trace tree.
