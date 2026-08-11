from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
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


# ── Хэши материала снапшота ───────────────────────────────────────────────
# Формулы живут ЗДЕСЬ и переиспользуются валидатором
# (`app/snapshot/validator.py`). Если считать хэш при создании одним кодом, а
# перепроверять другим — расчёты разъезжаются, и «рассинхрон снапшота» либо не
# ловится вообще, либо ловится ложно на каждом снапшоте. Материал берётся из
# payload-а снапшота (он же incident_data при создании), поэтому обе стороны
# видят одни и те же данные.


def compute_topology_hash(payload: Dict[str, Any]) -> str:
    """Хэш топологии инцидента: targets + policy_name."""
    material = str(payload.get("targets", [])) + str(payload.get("policy_name", ""))
    return sha256(material.encode("utf-8")).hexdigest()


def compute_metric_snapshot_hash(payload: Dict[str, Any]) -> str:
    """Хэш метрик снапшота (canonical JSON, sort_keys)."""
    return sha256(
        json.dumps(payload.get("metrics", {}), sort_keys=True).encode("utf-8")
    ).hexdigest()


def compute_log_window_hash(payload: Dict[str, Any]) -> str:
    """Хэш лог-окна снапшота (canonical JSON, sort_keys)."""
    return sha256(
        json.dumps(payload.get("logs", []), sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_snapshot_from_incident(
    incident_id: str,
    incident_data: Dict[str, Any],
    model_version: str,
    runtime_version: str,
    policy_decisions: Optional[Dict[str, Any]] = None,
) -> IncidentSnapshotV1:
    event_id = str(incident_data.get("incident_id", incident_id))
    topology_hash = compute_topology_hash(incident_data)
    metric_snapshot_hash = compute_metric_snapshot_hash(incident_data)
    log_window_hash = compute_log_window_hash(incident_data)
    related = [
        str(t.get("id"))
        for t in incident_data.get("targets", [])
        if isinstance(t, dict) and t.get("id") is not None
    ]
    return IncidentSnapshotV1(
        snapshot_id=f"snap-{incident_id}-{int(datetime.now(timezone.utc).timestamp())}",
        incident_id=incident_id,
        source_event_ids=[event_id],
        timestamps={
            "captured_at": _utc_now_iso(),
            "incident_ts": str(incident_data.get("timestamp", "")),
        },
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
