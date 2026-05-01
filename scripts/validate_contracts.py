#!/usr/bin/env python3
"""Validate control-plane JSON contracts for syntax and minimal example instances."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = {
    "snapshot": ROOT / "contracts" / "snapshot.v1.json",
    "budget": ROOT / "contracts" / "budget.v1.json",
    "ledger": ROOT / "contracts" / "ledger.v1.json",
    "routing": ROOT / "contracts" / "routing-policy.v1.json",
    "breaker": ROOT / "contracts" / "breaker.v1.json",
}

SAMPLES = {
    "snapshot": {
        "schema_version": "snapshot.v1",
        "incident_id": "inc-123",
        "captured_at": "2026-05-01T00:00:00Z",
        "environment_fingerprint": "env-abcdef12",
        "observability_fingerprint": "obs-abcdef12",
        "context": {},
        "artifacts": {"logs": [], "metrics": [], "events": []},
    },
    "budget": {
        "schema_version": "budget.v1",
        "incident_id": "inc-123",
        "state": "OK",
        "version": 0,
        "caps": {"calls": 10, "tokens": 1000, "cost": 1.0, "replay_cost": 0.2, "replay_time_ms": 5000},
        "consumption": {"calls": 0, "tokens": 0, "cost": 0.0, "replay_cost": 0.0, "replay_time_ms": 0},
    },
    "ledger": {
        "schema_version": "ledger.v1",
        "entry_id": "le-1",
        "incident_id": "inc-123",
        "usage_event_id": "ue-1",
        "pricing_version": "v1",
        "cost": 0.01,
        "recorded_at": "2026-05-01T00:00:00Z",
    },
    "routing": {
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
    "breaker": {
        "schema_version": "breaker.v1",
        "state": "NORMAL",
        "signals": {"cost_burn": 0, "token_burn": 0, "queue_backlog": 0, "error_rate": 0, "latency": 0},
        "hysteresis": {"cooldown_seconds": 60, "min_state_hold_seconds": 300},
    },
}


def main() -> int:
    failures = []
    for name, path in CONTRACTS.items():
        try:
            schema = json.loads(path.read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(instance=SAMPLES[name], schema=schema)
            print(f"[OK] {name}: {path}")
        except Exception as exc:  # noqa: BLE001
            failures.append((name, str(exc)))
            print(f"[FAIL] {name}: {exc}")

    if failures:
        print("\nContract validation failed:")
        for name, err in failures:
            print(f"- {name}: {err}")
        return 1

    print("\nAll contracts validated successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
