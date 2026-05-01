from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any, Dict, List

from app.snapshot.schema import IncidentSnapshotV1


REQUIRED_FIELDS = [
    "snapshot_id",
    "incident_id",
    "timestamps",
    "source_event_ids",
    "topology_hash",
    "metric_snapshot_hash",
    "log_window_hash",
    "ingest_time_source",
    "correlation_index",
]


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def validate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    for field in REQUIRED_FIELDS:
        if field not in snapshot or snapshot.get(field) is None:
            errors.append(f"missing required field: {field}")

    source_event_ids = snapshot.get("source_event_ids") or []
    if len(source_event_ids) == 0:
        errors.append("source_event_ids must not be empty")

    timestamps = snapshot.get("timestamps") or {}
    ts_values = [v for v in timestamps.values() if isinstance(v, str) and v]
    parsed = [_parse_iso(v) for v in ts_values]
    parsed_valid = [p for p in parsed if p is not None]
    if parsed_valid and len(parsed_valid) >= 2:
        if min(parsed_valid) > max(parsed_valid):
            errors.append("timestamps.min > timestamps.max")
        if parsed_valid != sorted(parsed_valid):
            warnings.append("non-monotonic timestamps detected")

    payload = snapshot.get("payload") or {}
    expected_metric_hash = sha256(json.dumps(payload.get("metrics", {}), sort_keys=True).encode("utf-8")).hexdigest()
    expected_log_hash = sha256(json.dumps(payload.get("logs", []), sort_keys=True).encode("utf-8")).hexdigest()

    if snapshot.get("metric_snapshot_hash") != expected_metric_hash:
        errors.append("metric_snapshot_hash mismatch")
    if snapshot.get("log_window_hash") != expected_log_hash:
        errors.append("log_window_hash mismatch")

    correlation_index = snapshot.get("correlation_index") or []
    source_set = set(str(i) for i in source_event_ids)
    for i, rel in enumerate(correlation_index):
        event_id = str(rel.get("event_id", ""))
        related_to = [str(x) for x in rel.get("related_to", [])]
        if event_id not in source_set:
            errors.append(f"correlation_index[{i}] invalid event_id")
        if not set(related_to).issubset(source_set):
            errors.append(f"correlation_index[{i}] related_to must be subset of source_event_ids")

    if not snapshot.get("ingest_time_source"):
        errors.append("ingest_time_source is required")

    status = "PASS"
    confidence = "HIGH"
    if errors:
        status = "FAIL"
        confidence = "LOW"
    elif warnings:
        status = "DEGRADED"
        confidence = "MEDIUM"

    return {
        "status": status,
        "reasons": errors,
        "warnings": warnings,
        "confidence_replay_safe": confidence,
    }


def validate_snapshot_model(snapshot: IncidentSnapshotV1) -> Dict[str, Any]:
    return validate_snapshot(snapshot.model_dump())
