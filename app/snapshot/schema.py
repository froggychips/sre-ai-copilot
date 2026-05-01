from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IncidentSnapshotV1(BaseModel):
    schema_version: str = Field(default="snapshot.v1")
    snapshot_id: str
    incident_id: str
    source_event_ids: List[str]
    timestamps: Dict[str, str]
    topology_hash: str
    metric_snapshot_hash: str
    log_window_hash: str
    model_version: str
    runtime_version: str
    policy_decisions: Dict[str, Any] = Field(default_factory=dict)
    correlation_index: List[Dict[str, Any]] = Field(default_factory=list)
    ingest_time_source: str = Field(default="collector-node-clock")
    payload: Dict[str, Any] = Field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_snapshot_from_incident(
    incident_id: str,
    incident_data: Dict[str, Any],
    model_version: str,
    runtime_version: str,
    policy_decisions: Optional[Dict[str, Any]] = None,
) -> IncidentSnapshotV1:
    event_id = str(incident_data.get("incident_id", incident_id))
    topology_material = str(incident_data.get("targets", [])) + str(incident_data.get("policy_name", ""))
    topology_hash = sha256(topology_material.encode("utf-8")).hexdigest()
    metric_snapshot_hash = sha256(json.dumps(incident_data.get("metrics", {}), sort_keys=True).encode("utf-8")).hexdigest()
    log_window_hash = sha256(json.dumps(incident_data.get("logs", []), sort_keys=True).encode("utf-8")).hexdigest()
    related = [str(t.get("id")) for t in incident_data.get("targets", []) if isinstance(t, dict) and t.get("id") is not None]
    return IncidentSnapshotV1(
        snapshot_id=f"snap-{incident_id}-{int(datetime.now(timezone.utc).timestamp())}",
        incident_id=incident_id,
        source_event_ids=[event_id],
        timestamps={"captured_at": _utc_now_iso(), "incident_ts": str(incident_data.get("timestamp", ""))},
        topology_hash=topology_hash,
        metric_snapshot_hash=metric_snapshot_hash,
        log_window_hash=log_window_hash,
        model_version=model_version,
        runtime_version=runtime_version,
        policy_decisions=policy_decisions or {},
        correlation_index=[{"event_id": event_id, "related_to": related}],
        ingest_time_source="collector-node-clock",
        payload=incident_data,
    )
