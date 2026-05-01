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


def test_validate_contract_payload_rejects_unknown_field():
    payload = {
        "schema_version": "budget.v1",
        "incident_id": "inc-1",
        "state": "OK",
        "version": 0,
        "caps": {"calls": 1, "tokens": 1, "cost": 1, "replay_cost": 1, "replay_time_ms": 1},
        "consumption": {"calls": 0, "tokens": 0, "cost": 0, "replay_cost": 0, "replay_time_ms": 0},
        "extra": "nope",
    }
    import jsonschema
    with pytest.raises(jsonschema.ValidationError):
        validate_contract_payload("budget.v1", payload)
