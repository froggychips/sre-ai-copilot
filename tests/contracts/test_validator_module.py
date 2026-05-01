import pytest

from app.contracts.validator import validate_contract_payload


def test_validate_contract_payload_success():
    payload = {
        "schema_version": "snapshot.v1",
        "incident_id": "inc-1",
        "captured_at": "2026-05-01T00:00:00Z",
        "environment_fingerprint": "env-abcdef12",
        "observability_fingerprint": "obs-abcdef12",
        "context": {},
        "artifacts": {"logs": [], "metrics": [], "events": []},
    }
    validate_contract_payload("snapshot.v1", payload)


def test_validate_contract_payload_unknown_schema():
    with pytest.raises(ValueError):
        validate_contract_payload("unknown.v1", {})
