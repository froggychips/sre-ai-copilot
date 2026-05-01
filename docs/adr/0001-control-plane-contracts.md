# ADR 0001: Control-Plane Contracts, Versioning, and Rollback

## Status
Accepted

## Context
The incident engine requires deterministic replay, strict budget governance, and auditable cost controls. To prevent runtime drift and policy bypass, all control-plane behavior must be contract-driven.

## Decision
1. **Contract-first control plane**: runtime components must only exchange payloads defined by:
   - `contracts/snapshot.v1.json`
   - `contracts/budget.v1.json`
   - `contracts/ledger.v1.json`
   - `contracts/routing-policy.v1.json`
   - `contracts/breaker.v1.json`
2. **No-bypass rule**: direct execution paths that skip validation, budget checks, or breaker enforcement are forbidden.
3. **Versioning policy**:
   - Semantic contract names with explicit major (`*.v1.json`).
   - Backward-incompatible changes require new major contract (`v2`).
   - Deprecation window: one release cycle minimum for old major.
4. **Rollback policy**:
   - Policy rollback via pointer switch to last known-good version.
   - Contract rollback is allowed only to versions with migration-safe payload compatibility.
   - Rollback actions must produce audit entries with actor, reason, and timestamp.

## Consequences
- **Positive**: predictable behavior under load, reproducible replay, auditable cost chain.
- **Tradeoff**: additional governance overhead for schema evolution and validation gates.

## Implementation Notes
- CI should validate JSON schema syntax and required file presence.
- Runtime should reject unknown fields where `additionalProperties=false` is set.
- Breaker and routing policies must expose version and source in logs.
