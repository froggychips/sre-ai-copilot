import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "schema_file, sample",
    [
        (
            "snapshot.v1.json",
            {
                "schema_version": "snapshot.v1",
                "incident_id": "inc-1",
                "captured_at": "2026-05-01T00:00:00Z",
                "environment_fingerprint": "env-abcdef12",
                "observability_fingerprint": "obs-abcdef12",
                "context": {},
                "artifacts": {"logs": [], "metrics": [], "events": []},
            },
        ),
        (
            "budget.v1.json",
            {
                "schema_version": "budget.v1",
                "incident_id": "inc-1",
                "state": "OK",
                "version": 0,
                "caps": {"calls": 10, "tokens": 1000, "cost": 1, "replay_cost": 1, "replay_time_ms": 1000},
                "consumption": {
                    "calls": 0,
                    "tokens": 0,
                    "cost": 0,
                    "replay_cost": 0,
                    "replay_time_ms": 0,
                },
            },
        ),
        (
            "ledger.v1.json",
            {
                "schema_version": "ledger.v1",
                "entry_id": "le-1",
                "incident_id": "inc-1",
                "usage_event_id": "ue-1",
                "pricing_version": "v1",
                "cost": 0.1,
                "recorded_at": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "routing-policy.v1.json",
            {
                "schema_version": "routing-policy.v1",
                "mode": "NORMAL",
                "risk_weights": {
                    "severity": 1,
                    "blast_radius": 1,
                    "change_velocity": 1,
                    "pattern_confidence": 1,
                },
                "safe_bias": {"default_route": "SHORT", "allow_full_on_uncertainty": False},
            },
        ),
        (
            "breaker.v1.json",
            {
                "schema_version": "breaker.v1",
                "state": "NORMAL",
                "signals": {"cost_burn": 1, "token_burn": 1, "queue_backlog": 1, "error_rate": 0.1, "latency": 10},
                "hysteresis": {"cooldown_seconds": 30, "min_state_hold_seconds": 60},
            },
        ),
    ],
)
def test_contract_schema_and_sample(schema_file, sample):
    schema_path = ROOT / "contracts" / schema_file
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(instance=sample, schema=schema)


def test_budget_contract_rejects_unknown_field():
    schema = json.loads((ROOT / "contracts" / "budget.v1.json").read_text())
    payload = {
        "schema_version": "budget.v1",
        "incident_id": "inc-1",
        "state": "OK",
        "version": 0,
        "caps": {"calls": 10, "tokens": 1000, "cost": 1, "replay_cost": 1, "replay_time_ms": 1000},
        "consumption": {"calls": 0, "tokens": 0, "cost": 0, "replay_cost": 0, "replay_time_ms": 0},
        "unexpected": True,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)
