import pytest

from app.replay.integrity import evaluate_snapshot_integrity


def _base_snapshot() -> dict:
    return {
        "schema_version": "snapshot.v1",
        "incident_id": "inc-1",
        "captured_at": "2026-05-01T00:00:00Z",
        "environment_fingerprint": "env-abcdef12",
        "observability_fingerprint": "obs-abcdef12",
        "context": {},
        "artifacts": {"logs": [], "metrics": [], "events": []},
    }


def test_integrity_gate_pass_routes_full():
    result = evaluate_snapshot_integrity(_base_snapshot())
    assert result.status == "PASS"
    assert result.mode == "FULL"


def test_integrity_gate_degraded_routes_low_fidelity():
    payload = _base_snapshot()
    payload["integrity_status"] = "DEGRADED"
    result = evaluate_snapshot_integrity(payload)
    assert result.status == "DEGRADED"
    assert result.mode == "LOW_FIDELITY"


def test_integrity_gate_fail_blocks_replay():
    payload = _base_snapshot()
    payload["integrity_status"] = "FAIL"
    result = evaluate_snapshot_integrity(payload)
    assert result.status == "FAIL"
    assert result.mode == "BLOCKED"


def test_integrity_gate_rejects_invalid_snapshot_schema():
    payload = _base_snapshot()
    del payload["incident_id"]
    with pytest.raises(Exception):
        evaluate_snapshot_integrity(payload)
