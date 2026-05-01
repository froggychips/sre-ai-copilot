# Control-Plane No-Bypass Policy

## Prohibited Runtime Paths
The following are forbidden in production execution paths:
- invoking control-plane stages without contract validation,
- updating budget state outside Budget Controller flow,
- writing priced ledger entries without pricing version,
- routing/execution that skips breaker state enforcement.

## Enforcement
- Contract checks (`scripts/validate_contracts.py` + `pytest -q tests/contracts`) are merge gates.
- CI workflow `.github/workflows/contracts-validation.yml` must pass on contract-related changes.
- Runtime entry points must call `validate_contract_payload(...)` for relevant control-plane payloads.

## Break-Glass
- Emergency override allowed only by SRE with audit trail and ticket link.
- Overrides auto-expire and require post-incident review.
