# FINAL ROADMAP v1 — Execution Backlog (Task Decomposition)

## Scope
This document decomposes the approved roadmap into execution-ready tasks with owners, dependencies, deliverables, and Definition of Done (DoD).

---

## Phase 0 — Contracts + Control Plane Definition (2–3 days) ✅ COMPLETED
**Owner:** Tech Lead + Staff Backend + SRE Lead  
**Dependencies:** none

### Tasks
- [x] Define `snapshot.v1` JSON schema and required immutable fields.
- [x] Define `budget.v1` JSON schema including caps and state constraints.
- [x] Define `ledger.v1` JSON schema for cost authority and auditability.
- [x] Define `routing-policy.v1` JSON schema for risk routing and mode restrictions.
- [x] Define `breaker.v1` JSON schema for global states and trigger signals.
- [x] Document versioning policy (forward/backward compatibility, deprecation windows).
- [x] Document rollback policy (policy pointer rollback + schema rollback constraints).
- [x] Document and enforce “no bypass” requirements.
- [x] Write ADR `0001-control-plane-contracts.md`.

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

---

## RACI Matrix (Cross-Team Ownership)

Legend: **R** = Responsible, **A** = Accountable, **C** = Consulted, **I** = Informed.

| Workstream / Decision Area | Tech Lead | Backend | Platform | SRE | Product | Analytics |
|---|---|---|---|---|---|---|
| Contract schema changes (`snapshot/budget/ledger/routing/breaker`) | A | R | C | C | C | I |
| Contract versioning/deprecation policy | A | R | C | C | C | I |
| Snapshot integrity gate behavior | C | A/R | C | C | I | I |
| Replay data firewall policy | C | R | C | A | I | I |
| Budget state machine transitions/caps | C | A/R | C | C | I | C |
| Pricing table version updates | A | R | C | I | C | C |
| Reconciliation loop + drift policy | C | R | A | C | I | C |
| Risk routing scoring function | C | A/R | C | C | C | I |
| Degraded/protected policy semantics | A | C | C | R | C | I |
| Single-flight locking and contention controls | I | R | A | C | I | I |
| Retry policy matrix updates | C | R | A | C | I | I |
| Global breaker thresholds/hysteresis | C | C | R | A | C | I |
| Manual override activation | I | I | C | A/R | C | I |
| Rollback pointer/hotfix execution | C | R | A | C | I | I |
| SLO auto-actions policy | C | C | R | A | C | C |
| ROI baseline definition | C | I | I | C | A | R |
| Executive demo acceptance | A | C | C | C | R | C |

### Escalation & Approval Boundaries

#### Emergency policy rollback
- **Authority:** SRE (A) can trigger rollback immediately in incident context.
- **Execution:** Platform (R) applies rollback pointer/hotfix.
- **Post-action review:** TL + Backend + Product within next business day.

#### Breaker manual override
- **Authority:** SRE (A/R) during active incident.
- **Guardrails:** Mandatory audit trail + incident ticket link.
- **Cooldown:** Return-to-normal requires SRE + Platform concurrence.

#### Pricing update freeze
- **Authority:** Tech Lead (A) can freeze pointer on drift anomalies.
- **Execution:** Backend (R) applies freeze; Platform (C) validates reconciliation jobs.

#### Go/No-Go decision rights
- **GO requires unanimous sign-off:** TL, Backend Owner, Platform Owner, SRE Owner.
- **Product veto:** Product can veto GO for unmet business readiness/ROI methodology.

### Handoff SLAs (Inter-team)
| Handoff | SLA | Evidence |
|---|---|---|
| Contract change proposal -> review decision | <= 2 business days | ADR/comment resolution log |
| Drift alert -> reconciliation action started | <= 30 minutes | Alert timeline + job run ID |
| Breaker PROTECTED -> operator acknowledgment | <= 10 minutes | Pager ack + incident timeline |
| Rollback request -> rollback applied | <= 15 minutes | Change event + audit entry |
| SLO breach -> auto-governance action verified | <= 20 minutes | Metric event + policy action log |

---

## Resilience Test Plan (Load / Chaos / Isolation)

### Test Scope
Validate that cost control, replay isolation, routing safety, and breaker stability hold under stress and partial failures.

### Environment Strategy
- **Stage A (Pre-prod deterministic tests):** contract + replay determinism + ledger correctness.
- **Stage B (Canary load tests):** mixed live/replay synthetic incidents with production-like traffic profile.
- **Stage C (Failure-injection tests):** targeted control-plane, queue, and dependency disruptions.

### Scenario Matrix
| ID | Scenario | Injection / Load | Expected Outcome | Blocking Severity |
|---|---|---|---|---|
| LT-01 | Incident burst storm | 3–5x normal incident arrival for 30 min | No runaway cost, breaker converges, no flapping | Blocker |
| LT-02 | Replay flood | Replay queue saturation with bounded budget | Live latency/SLA preserved, replay throttled | Blocker |
| LT-03 | Retry amplification | Force transient failures on hot stage | Retry caps applied, retry spend bounded | High |
| LT-04 | Single-flight contention | Concurrent same `(incident_id,stage)` submissions | Exactly one execution path, duplicates suppressed | Blocker |
| LT-05 | Ledger drift simulation | Inject delayed/duplicate usage events | Reconciliation catches drift and triggers actions | High |
| LT-06 | Control-plane partial outage | Disable dynamic policy service for 10 min | Static fallback mode engages safely | Blocker |
| LT-07 | Breaker trigger/recover | Oscillating latency+error signal | Hysteresis prevents oscillation | High |
| LT-08 | Replay firewall violation attempt | Simulate live external call from replay path | Call blocked, policy violation logged | Blocker |
| LT-09 | Checkpoint/resume | Kill execution mid-stage | Resume from checkpoint without duplicate spend | High |
| LT-10 | Pricing pointer mismatch | Swap pricing version mid-run | Write rejection + auditable error path | High |

### Required Metrics Per Test
- Cost: total, per-incident p95, replay cost share.
- Safety: budget terminations, blocked over-budget calls, firewall violations.
- Stability: queue depth, breaker transitions/hour, error rate, p95 latency.
- Correctness: deterministic replay match rate, duplicate-stage count, ledger drift %.
- Recovery: rollback time, fallback activation time, checkpoint resume success rate.

### Pass/Fail Criteria
- **PASS** only if all blocker scenarios pass and no hard-action policy is violated.
- **CONDITIONAL PASS** if only High severity scenarios fail with approved mitigation + deadline.
- **FAIL** if any blocker fails, breaker flaps above threshold, or drift exceeds threshold.

### Evidence Artifacts
- Test run manifest (scenario IDs, timestamps, versions).
- Metric export snapshots and dashboard captures.
- Policy decision logs (routing, breaker, fallback, rollback).
- Ledger reconciliation report and drift deltas.
- Incident timeline for each failed scenario with RCA owner.

### Execution Cadence
- Pre-merge (critical policy changes): LT-04, LT-08, LT-10 smoke set.
- Pre-release candidate: full LT-01..LT-10 suite.
- Post-release canary: LT-01, LT-02, LT-07, LT-06 daily for 3 days.
- Monthly resilience drill: full suite + updated thresholds review.

### Ownership
| Test Area | Primary Owner | Backup Owner |
|---|---|---|
| Load generation & orchestration | Platform | SRE |
| Policy validation (routing/breaker) | SRE | Backend |
| Budget/ledger correctness | Backend | Platform |
| ROI/cost interpretation | Analytics | Product |

### Exit Rule for Production Promotion
Production promotion is allowed only when:
- Last full resilience suite status is PASS.
- No unresolved blocker defects remain open.
- Go/No-Go committee confirms evidence completeness.

---

## Sprint-Ready Backlog (Jira-Style Epics & Stories)

### Estimation Legend
- **XS**: 0.5–1 day
- **S**: 1–2 days
- **M**: 3–4 days
- **L**: 5–7 days
- **XL**: 8–10 days

### EPIC CP-0 — Contract Foundation
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP0-1 | Define `snapshot.v1` schema + validator | M | none | Schema merged, validator enforces required fields |
| CP0-2 | Define `budget.v1` schema | S | CP0-1 | Budget caps and states represented in schema |
| CP0-3 | Define `ledger.v1` schema | S | CP0-2 | Ledger fields support full audit chain |
| CP0-4 | Define `routing-policy.v1` and `breaker.v1` schemas | M | CP0-1 | Policies parse/validate and include version metadata |
| CP0-5 | ADR-0001 contracts/versioning/rollback | S | CP0-1..CP0-4 | ADR approved by TL/Backend/SRE/Product |

### EPIC CP-1 — Replay Safety
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP1-1 | Add snapshot fingerprints (`observability`, `environment`) | S | CP0-1 | Fields are immutable and validated |
| CP1-2 | Implement snapshot integrity gate | M | CP1-1 | FAIL/DEGRADED/PASS branches covered by tests |
| CP1-3 | Implement replay data firewall | M | CP1-2 | Replay cannot perform live external calls |
| CP1-4 | Integration tests for deterministic replay | M | CP1-2, CP1-3 | Determinism match rate >= target |

### EPIC CP-2 — Budget & Ledger Authority
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP2-1 | Budget state machine + single writer | L | CP0-2 | Only Budget Controller mutates budget state |
| CP2-2 | Atomic CAS transitions + stale write rejection | M | CP2-1 | Concurrent updates safely rejected |
| CP2-3 | Hard caps enforcement (`calls/tokens/cost/replay_*`) | M | CP2-1 | Over-budget requests are blocked |
| CP2-4 | Input fingerprint dedup/idempotency | M | CP2-2 | Duplicate execution suppressed |
| CP2-5 | Cost authority chain implementation | L | CP0-3, CP2-1 | UsageEvent -> Pricing(vX) -> Ledger traceable |
| CP2-6 | Reconciliation job + drift alerts | M | CP2-5 | Drift alerts fire and route correctly |

### EPIC CP-3 — Routing & Degraded Controls
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP3-1 | Risk score calculation (`severity/blast/change/confidence`) | M | CP2-1 | Score reproducible and logged |
| CP3-2 | Safe-bias routing defaults | S | CP3-1 | Unknown/ambiguous never default to full |
| CP3-3 | Degraded policy enforcement | M | CP3-2 | No full pipeline / no learning write in degraded |
| CP3-4 | Golden scenario policy tests | M | CP3-2, CP3-3 | Test suite passes with deterministic outcomes |

### EPIC CP-35 — Runtime Contention & Resilience
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP35-1 | Single-flight lock per `(incident_id,stage)` | M | CP2-2 | Duplicate stage execution = 0 |
| CP35-2 | Retry policy matrix + retry budget isolation | M | CP2-3 | Retry caps and spend attribution enforced |
| CP35-3 | Replay queue/pool isolation + quotas | L | CP35-1 | Live SLA unaffected by replay load |
| CP35-4 | Policy rollback/hotfix mechanism | M | CP35-2 | Rollback applied within SLA |

### EPIC CP-38 — Determinism & Fallback
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP38-1 | Incident event sequencing protocol | M | CP35-1 | Ordered processing reproducible |
| CP38-2 | Control-plane static fallback mode | M | CP38-1 | Fallback engages automatically on outage |
| CP38-3 | Checkpoint/resume for partial execution | L | CP38-1 | Resume success rate >= target |
| CP38-4 | Anti-feedback learning gate | S | CP38-2 | Replay does not contaminate learning writes |

### EPIC CP-4 — Global Breaker
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP4-1 | Implement NORMAL/DEGRADED/PROTECTED states | M | CP2-6 | State transitions auditable |
| CP4-2 | Multi-signal triggers + weighting | M | CP4-1 | Triggers include cost, queue, error, latency |
| CP4-3 | Hysteresis and cooldown logic | S | CP4-2 | Flapping remains below target |
| CP4-4 | Manual override + audit trail | S | CP4-1 | Override actions are fully logged |

### EPIC CP-5 — Replay Governance
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP5-1 | Bounded replay limits (`cost/tokens/time/runs`) | M | CP1-3, CP2-3 | Replay hard limits enforced |
| CP5-2 | Replay QoS policy (low priority) | S | CP35-3 | Live priority maintained |
| CP5-3 | Learning-write gate in replay paths | S | CP38-4 | No replay learning writes without allow policy |

### EPIC CP-6 — Observability & ROI
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP6-1 | Cost/usage dashboards | M | CP2-6 | Required cost panels available |
| CP6-2 | SLO auto-governance actions wiring | M | CP4-2 | Actions trigger from threshold conditions |
| CP6-3 | ROI baseline and reporting definitions | M | CP6-1 | Baseline approved by Product/Analytics |

### EPIC CP-7 — Demo & Executive Narrative
| Story ID | Story | Size | Dependencies | Acceptance Criteria |
|---|---|---:|---|---|
| CP7-1 | Incident-to-recovery scenario script | S | CP3-4, CP4-3 | Script repeatable without manual intervention |
| CP7-2 | Controlled execution + approval checkpoints | S | CP7-1 | Approval gates visible in demo output |
| CP7-3 | ROI/cost report overlay for demo | S | CP6-3 | Report auto-generated from collected metrics |

### Suggested Sprint Sequencing (first 4 sprints)
- **Sprint 1:** CP0-1..CP0-5, CP1-1
- **Sprint 2:** CP1-2..CP1-4, CP2-1, CP2-2
- **Sprint 3:** CP2-3..CP2-6, CP3-1, CP3-2
- **Sprint 4:** CP3-3, CP3-4, CP35-1, CP35-2, CP4-1
