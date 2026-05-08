# Control-Plane Policies

This document consolidates the versioning, rollback, and no-bypass policies
for control-plane contracts under `contracts/`.

## 1. Versioning

### Scope
Applies to control-plane contracts in `contracts/`:
- `snapshot.v*.json`
- `budget.v*.json`
- `ledger.v*.json`
- `routing-policy.v*.json`
- `breaker.v*.json`

### Rules
1. Backward-incompatible contract changes MUST increment major version (`v1` -> `v2`).
2. New required fields in existing major are prohibited unless default-compatible.
3. Existing field type changes in same major are prohibited.
4. Contract `schema_version` const must match file name major.
5. Deprecation window for old major: at least one release cycle.

### Release Requirements
- Both new and previous major validators must pass during overlap window.
- Routing/breaker logs must emit active contract version.

## 2. Rollback

### Rollback Types
1. **Policy pointer rollback**: switch to last known-good policy config/version.
2. **Contract rollback**: only allowed to migration-safe compatible major.

### Mandatory Controls
All rollback actions require:
- incident/change ticket reference,
- actor identity,
- UTC timestamp,
- rollback reason.

Rollback must be auditable and reversible.

### Runtime Guarantees
- Rollback must not bypass budget validation, contract validation, or breaker checks.
- If uncertainty remains, system must enter `PROTECTED` mode until validated.

## 3. No-Bypass

### Prohibited Runtime Paths
The following are forbidden in production execution paths:
- invoking control-plane stages without contract validation,
- updating budget state outside Budget Controller flow,
- writing priced ledger entries without pricing version,
- routing/execution that skips breaker state enforcement.

### Enforcement
- Contract checks (`scripts/validate_contracts.py` + `pytest -q tests/contracts`) are merge gates.
- CI workflow `.github/workflows/contracts-validation.yml` must pass on contract-related changes.
- Runtime entry points must call `validate_contract_payload(...)` for relevant control-plane payloads.

### Break-Glass
- Emergency override allowed only by SRE with audit trail and ticket link.
- Overrides auto-expire and require post-incident review.
