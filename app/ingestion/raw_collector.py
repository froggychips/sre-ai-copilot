from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List


class RawCollector:
    """Append-only in-memory raw ingestion buffer with simple dedup."""

    def __init__(self):
        self._events: List[Dict[str, Any]] = []
        self._seen_ids = set()
        self._lock = Lock()

    def ingest(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(event.get("incident_id") or event.get("id") or "")
        if not event_id:
            raise ValueError("event id is required")

        with self._lock:
            if event_id in self._seen_ids:
                return {"status": "duplicate", "event_id": event_id}

            self._seen_ids.add(event_id)
            self._events.append(
                {
                    "event_id": event_id,
                    "ingest_ts": datetime.now(timezone.utc).isoformat(),
                    "ingest_time_source": "collector-node-clock",
                    "raw": event,
                }
            )
        return {"status": "accepted", "event_id": event_id, "ingest_time_source": "collector-node-clock"}


raw_collector = RawCollector()
