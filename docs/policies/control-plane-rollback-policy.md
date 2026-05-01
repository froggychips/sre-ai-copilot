# Control-Plane Rollback Policy

## Rollback Types
1. **Policy pointer rollback**: switch to last known-good policy config/version.
2. **Contract rollback**: only allowed to migration-safe compatible major.

## Mandatory Controls
- All rollback actions require:
  - incident/change ticket reference,
  - actor identity,
  - UTC timestamp,
  - rollback reason.
- Rollback must be auditable and reversible.

## Runtime Guarantees
- Rollback must not bypass budget validation, contract validation, or breaker checks.
- If uncertainty remains, system must enter `PROTECTED` mode until validated.
