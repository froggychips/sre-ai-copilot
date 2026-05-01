# FINAL ROADMAP v1 — Execution Backlog (Task Decomposition)

## Scope
This document decomposes the approved roadmap into execution-ready tasks with owners, dependencies, deliverables, and Definition of Done (DoD).

---

## Phase 0 — Contracts + Control Plane Definition (2–3 days)
**Owner:** Tech Lead + Staff Backend + SRE Lead  
**Dependencies:** none

### Tasks
- [ ] Define `snapshot.v1` JSON schema and required immutable fields.
- [ ] Define `budget.v1` JSON schema including caps and state constraints.
- [ ] Define `ledger.v1` JSON schema for cost authority and auditability.
- [ ] Define `routing-policy.v1` JSON schema for risk routing and mode restrictions.
- [ ] Define `breaker.v1` JSON schema for global states and trigger signals.
- [ ] Document versioning policy (forward/backward compatibility, deprecation windows).
- [ ] Document rollback policy (policy pointer rollback + schema rollback constraints).
- [ ] Document and enforce “no bypass” requirements.
- [ ] Write ADR `0001-control-plane-contracts.md`.

### Deliverables
- `contracts/snapshot.v1.json`
- `contracts/budget.v1.json`
- `contracts/ledger.v1.json`
- `contracts/routing-policy.v1.json`
- `contracts/breaker.v1.json`
- `docs/adr/0001-control-plane-contracts.md`

### Definition of Done
- Contracts signed off by Backend, SRE, Product.
- No runtime behavior outside published contracts.

---

## Phase 1 — Snapshot Integrity + Data Firewall (3–7 days)
**Owner:** Backend Team  
**Dependencies:** Phase 0

### Tasks
- [ ] Finalize immutable snapshot field list.
- [ ] Add `observability_fingerprint` to snapshot payload.
- [ ] Add `environment_fingerprint` to snapshot payload.
- [ ] Implement Snapshot Integrity Gate:
  - FAIL -> block
  - DEGRADED -> low-fidelity only
  - PASS -> full replay
- [ ] Implement Replay Data Firewall:
  - snapshot-only inputs
  - no live external calls during replay
- [ ] Add integration tests for all gate branches.

### Deliverables
- Updated snapshot schema + validator
- Replay isolation enforcement policy
- Integration tests for gate behavior

### Definition of Done
- Replay is a deterministic function of snapshot.
- No bypass path for replay data firewall.

---

## Phase 2 — Budget Engine (Atomic State Machine) (5–10 days)
**Owner:** Backend + Platform  
**Dependencies:** Phase 0, Phase 1

### Tasks
- [ ] Implement budget state machine transitions:
  `OK -> WARN -> EXHAUSTED -> TERMINATED`.
- [ ] Enforce single writer pattern (Budget Controller only).
- [ ] Implement atomic CAS transitions and rejection on stale version.
- [ ] Enforce hard caps: `calls`, `tokens`, `cost`, `replay_cost`, `replay_time`.
- [ ] Implement input fingerprint dedup for idempotent suppression.
- [ ] Implement cost authority chain:
  `UsageEvent -> CostAdapter -> PricingTable(vX) -> Ledger`.
- [ ] Introduce pricing table versioning and effective date rules.
- [ ] Build reconciliation loop (runtime vs ledger) and drift alerts.

### Deliverables
- Budget Controller module/service
- Cost Ledger + pricing versioning
- Reconciliation job + drift alerting

### Definition of Done
- Budget cannot be exceeded without termination.
- Every cost event is fully auditable end-to-end.
- Duplicate execution by fingerprint is suppressed.

---

## Phase 3 — Triage v2 + Risk Routing (5–7 days)
**Owner:** Backend + SRE  
**Dependencies:** Phase 2

### Tasks
- [ ] Implement risk score function:
  `severity + blast_radius + change_velocity + pattern_confidence`.
- [ ] Implement safe-bias routing:
  uncertainty -> short/hybrid, never full by default.
- [ ] Implement degraded policy restrictions:
  - no full pipeline
  - no learning write
  - replay low-fidelity only
- [ ] Add policy test suite with golden scenarios.

### Deliverables
- Routing engine config
- Golden scenario policy tests

### Definition of Done
- Unknown/ambiguous cases do not route to full by default.
- Degraded mode constraints are strictly enforced.

---

## Phase 3.5 — Runtime Contention & Policy Resilience (5–8 days)
**Owner:** Platform + Backend  
**Dependencies:** Phase 2, Phase 3

### Tasks
- [ ] Implement single-flight execution per `(incident_id, stage)`.
- [ ] Implement explicit retry governance:
  - max retries per stage
  - retry attribution in ledger
  - retry budget isolation
- [ ] Add incident-aware breaker weighting.
- [ ] Implement replay resource isolation:
  - separate queue/pool
  - CPU/memory quotas
  - priority `live > replay`
- [ ] Implement policy rollback/hotfix framework.
- [ ] Write rollback operator runbook.

### Deliverables
- Locking/serialization layer
- Retry policy matrix
- Policy rollback runbook

### Definition of Done
- No duplicate stage execution.
- Replay does not degrade live SLA.
- Policy rollback completes in minutes.

---

## Phase 3.8 — Determinism & Control-Plane Resilience (4–6 days)
**Owner:** Platform + Backend + SRE  
**Dependencies:** Phase 3.5

### Tasks
- [ ] Implement event sequencing per incident stream.
- [ ] Implement static fallback mode for control-plane outages.
- [ ] Implement checkpoint/resume for partial execution.
- [ ] Add ledger drift auto-actions.
- [ ] Add replay anti-feedback learning gate.

### Deliverables
- Sequencing protocol
- Fallback switch + health checks
- Checkpoint/resume runtime spec
- Learning gate policy

### Definition of Done
- Replay is reproducible by event order.
- Control-plane failures do not trigger cost explosion or freeze.
- Partial failures resume correctly.

---

## Phase 4 — Global Circuit Breaker (5–7 days)
**Owner:** Platform/SRE  
**Dependencies:** Phase 2, Phase 3.5, Phase 3.8

### Tasks
- [ ] Implement global states:
  `NORMAL -> DEGRADED -> PROTECTED`.
- [ ] Add multi-signal triggers:
  cost/token burn, queue backlog, error rate, latency, incident concentration.
- [ ] Enforce PROTECTED policy behavior.
- [ ] Implement hysteresis and cooldown windows.
- [ ] Add manual override with mandatory audit trail.
- [ ] Build SRE dashboard + runbook.

### Deliverables
- Breaker service + config
- SRE dashboard
- Breaker runbook

### Definition of Done
- No runaway cost under load spikes.
- No flapping state transitions.

---

## Phase 5 — Replay Governance (3–5 days)
**Owner:** Backend + SRE  
**Dependencies:** Phase 1, Phase 3.5, Phase 4

### Tasks
- [ ] Enforce bounded replay (`cost/tokens/time/runs`).
- [ ] Apply low-priority replay QoS.
- [ ] Enforce learning write gating in replay.

### Definition of Done
- Replay does not impact live traffic.
- Replay budget is strictly bounded.

---

## Phase 6 — Observability + ROI Control Plane (3–5 days)
**Owner:** SRE + Product Analytics  
**Dependencies:** Phase 2, Phase 4, Phase 5

### Tasks
- [ ] Build dashboards for:
  - `llm_calls`, `tokens`, `cost`
  - `cost_per_incident p95`
  - `cost_per_replay`
  - `ledger_runtime_drift`
  - `short/full ratio`
- [ ] Implement SLO auto-actions:
  - increase short-path ratio
  - reduce full threshold
  - enable protected mode
- [ ] Implement ROI metrics:
  - `time_saved`
  - `manual_steps_reduced`
  - `time-to-hypothesis/root-cause`

### Definition of Done
- SLO breaches trigger automatic governance actions.
- ROI calculated against agreed baseline methodology.

---

## Phase 7 — Demo Layer (Executive Narrative) (2–3 days)
**Owner:** Product + Tech Lead + SRE  
**Dependencies:** Phases 1–6

### Tasks
- [ ] Build repeatable `incident -> recovery` demo scenario.
- [ ] Include routing decision transparency.
- [ ] Include safety gates and human approval step.
- [ ] Include controlled execution trace.
- [ ] Include ROI + cost report output.

### Definition of Done
- Demo repeatable without manual hacks.
- Demonstrates quality and cost-control simultaneously.

---

## Global Go/No-Go Release Gate
Release to production only if all conditions hold:
- [ ] Hard budget caps block calls in over-budget cases.
- [ ] Replay is deterministic and isolated.
- [ ] Breaker is stable under load (no flapping).
- [ ] Ledger drift remains within allowed threshold.
- [ ] SLO auto-actions validated end-to-end.


---

## Release Gates (Operational Format)

### Gate G1 — Contracts Freeze (after Phase 0)
**Entry criteria**
- All contract files exist and validate against JSON schema lint.
- ADR `0001-control-plane-contracts.md` is approved by TL, Backend, SRE, Product.

**Evidence required**
- Contract validation report (CI artifact).
- Signed review checklist with approvers and timestamps.
- “No bypass” static checks report.

**Exit decision**
- `PASS`: Phase 1 implementation unlocked.
- `HOLD`: open blocking issues tracked with owner+ETA.

### Gate G2 — Deterministic Replay Safety (after Phase 1)
**Entry criteria**
- Snapshot integrity gate implemented for FAIL/DEGRADED/PASS branches.
- Replay data firewall prevents live external calls.

**Evidence required**
- Integration test results for all gate paths.
- Determinism test run showing stable outputs for same snapshot.
- Security/policy check proving no firewall bypass path.

**Exit decision**
- `PASS`: Phase 2 budget implementation unlocked.
- `HOLD`: replay remains restricted to low-fidelity and non-production path.

### Gate G3 — Cost Authority Correctness (after Phase 2)
**Entry criteria**
- Budget state machine and single writer are enabled.
- CAS transition conflicts correctly rejected.
- Reconciliation loop emits drift metrics and alerts.

**Evidence required**
- State machine transition test matrix.
- Ledger audit trace from UsageEvent to priced ledger row.
- Drift report for representative workload.

**Exit decision**
- `PASS`: Phase 3+ orchestration rollout allowed.
- `HOLD`: full pipeline disabled; short/hybrid path only.

### Gate G4 — Runtime Contention Resilience (after Phase 3.5/3.8)
**Entry criteria**
- Single-flight locking in place.
- Retry policy + budget isolation enforced.
- Sequencing/checkpoint-resume validated.

**Evidence required**
- Contention/load test report (duplicate stage rate, retry distribution).
- Partial-failure recovery scenarios with successful resume.
- Control-plane fallback mode activation/deactivation logs.

**Exit decision**
- `PASS`: breaker rollout and production canary allowed.
- `HOLD`: canary blocked until contention defects closed.

### Gate G5 — Breaker Stability & Production Readiness (after Phase 4–6)
**Entry criteria**
- Breaker states and hysteresis enabled.
- SLO auto-actions integrated with routing/protected mode.
- Replay QoS isolation confirmed.

**Evidence required**
- Stress test report with no flapping.
- Runbook dry-run records (manual override, rollback, protected mode).
- KPI dashboard snapshot (cost/incident, drift, short/full ratio).

**Exit decision**
- `GO`: production release authorized.
- `NO-GO`: remain in canary/protected mode until thresholds pass.

---

## Go/No-Go Checklist Template (Release Committee)

Use this checklist in release meetings; all items must be explicitly marked.

### A) Cost Safety
- [ ] Hard budget caps block over-budget calls in runtime tests.
- [ ] Retry budget isolation prevents recursive burn.
- [ ] Reconciliation drift is within approved threshold.

### B) Determinism & Replay Safety
- [ ] Replay output is deterministic for identical snapshot inputs.
- [ ] Replay data firewall blocks all live external dependencies.
- [ ] Replay workload is isolated (queue/pool/quotas) from live traffic.

### C) Runtime Stability
- [ ] No duplicate stage execution under contention tests.
- [ ] Breaker does not flap under burst and recovery patterns.
- [ ] Checkpoint/resume works for partial failures.

### D) Control Plane Operability
- [ ] Static fallback mode can be activated within runbook target time.
- [ ] Policy rollback/hotfix completes within runbook SLA.
- [ ] Manual override actions produce complete audit trail.

### E) Business Readiness
- [ ] SLO auto-actions validated end-to-end.
- [ ] ROI baseline and current measurement method approved.
- [ ] Executive demo scenario passes without manual hacks.

### Committee Sign-off
- [ ] Tech Lead
- [ ] Backend Owner
- [ ] Platform Owner
- [ ] SRE Owner
- [ ] Product Owner
- [ ] Analytics Owner

---

## KPI & Threshold Baseline v1 (Initial Operating Targets)

> Note: values below are starting targets for v1 and should be tuned after canary data.

### Cost & Budget Guardrails
| Metric | Target | Alert | Hard Action |
|---|---:|---:|---|
| Cost per incident (p95) | <= +20% vs approved baseline | > +25% | Enable PROTECTED mode + force short/hybrid |
| Cost per replay run | <= 15% of incident budget | > 20% | Stop replay run, mark `REPLAY_BUDGET_EXHAUSTED` |
| Budget cap breaches blocked | 100% | < 100% | Immediate release freeze |
| Retry-attributed spend share | <= 10% of incident cost | > 15% | Reduce retry limits for hot stages |

### Ledger Correctness & Drift
| Metric | Target | Alert | Hard Action |
|---|---:|---:|---|
| `ledger_runtime_drift` (daily) | <= 1.0% | > 1.5% | Switch to protected routing until reconciled |
| Unpriced usage events | 0 | > 0 | Block closeout + open Sev2 internal incident |
| Pricing-table version mismatch | 0 | > 0 | Reject write path with explicit error |

### Replay Determinism & Isolation
| Metric | Target | Alert | Hard Action |
|---|---:|---:|---|
| Deterministic replay match rate | >= 99.5% | < 99.0% | Disable full replay globally |
| Replay live-call violations | 0 | > 0 | Trigger policy violation and block replay |
| Replay impact on live p95 latency | <= +5% | > +8% | Move replay to strict low-priority queue only |

### Runtime Stability
| Metric | Target | Alert | Hard Action |
|---|---:|---:|---|
| Duplicate stage executions | 0 | > 0 | Enforce single-flight lockdown mode |
| Breaker state flapping | <= 2 transitions/hour | > 3/hour | Increase hysteresis/cooldown and hold rollout |
| Checkpoint resume success rate | >= 99% | < 98% | Disable auto-resume; require operator approval |

### Routing Quality & Safety
| Metric | Target | Alert | Hard Action |
|---|---:|---:|---|
| Unknown/ambiguous routed to full | 0% by default | > 0% | Force safe-bias override |
| Short/hybrid ratio under stress | >= 70% | < 60% | Auto-increase short-path ratio |
| Degraded-policy violations | 0 | > 0 | Enter PROTECTED + audit incident |

### SLO Auto-Governance Trigger Matrix
| Trigger Condition | Auto-Action 1 | Auto-Action 2 | Escalation |
|---|---|---|---|
| Cost burn > threshold for 10 min | Increase short-path ratio by +15% | Lower full-threshold by -10% | Page SRE on-call |
| Queue backlog > threshold for 15 min | Enter DEGRADED | Suspend full pipeline for low severity | Incident commander notified |
| Error rate > threshold for 5 min | Enter PROTECTED | Disable learning writes | Page Backend + Platform |
| Drift > threshold for 2 cycles | Freeze pricing update pointer | Force reconciliation priority job | Escalate to TL |

### KPI Ownership & Review Cadence
| Area | Primary Owner | Review Cadence | Reporting Forum |
|---|---|---|---|
| Cost/Budget | Backend + Product Analytics | Daily | Ops standup |
| Ledger/Drift | Platform | Daily | Reliability review |
| Replay Safety | SRE + Backend | Per release + weekly | SRE governance |
| Breaker Stability | SRE | Per canary wave | Release council |
| ROI Outcomes | Product Analytics + TL | Weekly | Executive review |
