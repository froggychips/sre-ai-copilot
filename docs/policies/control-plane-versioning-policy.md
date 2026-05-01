# Control-Plane Versioning Policy

## Scope
Applies to control-plane contracts in `contracts/`:
- `snapshot.v*.json`
- `budget.v*.json`
- `ledger.v*.json`
- `routing-policy.v*.json`
- `breaker.v*.json`

## Rules
1. Backward-incompatible contract changes MUST increment major version (`v1` -> `v2`).
2. New required fields in existing major are prohibited unless default-compatible.
3. Existing field type changes in same major are prohibited.
4. Contract `schema_version` const must match file name major.
5. Deprecation window for old major: at least one release cycle.

## Release Requirements
- Both new and previous major validators must pass during overlap window.
- Routing/breaker logs must emit active contract version.
