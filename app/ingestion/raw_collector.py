from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque, Dict

# Жёсткие потолки. Буфер живёт всю жизнь процесса и наполняется КАЖДЫМ
# webhook-batch'ем ещё ДО suppression/validation — раньше он рос бесконечно
# (append-only list + set), т.е. был гарантированным OOM-вектором: атакующему
# достаточно слать мусорные batch'и с уникальными id.
_MAX_EVENTS = 1000
_MAX_SEEN_IDS = 10_000


class RawCollector:
    """In-memory raw ingestion ring-buffer с простым dedup.

    Оба хранилища ограничены: `_events` — deque(maxlen), `_seen_ids` —
    ordered-set с вытеснением старейших id при переполнении.
    """

    def __init__(self):
        self._events: Deque[Dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        # OrderedDict как ordered-set: порядок вставки нужен для FIFO-eviction.
        self._seen_ids: "OrderedDict[str, None]" = OrderedDict()
        self._lock = Lock()

    def ingest(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(event.get("incident_id") or event.get("id") or "")
        if not event_id:
            # Пустой id (например, AM batch с пустым groupKey) — не повод
            # ронять весь webhook 500-кой (раньше тут был raise ValueError).
            # Выводим детерминированный id из содержимого — dedup продолжает
            # работать и для таких событий.
            try:
                material = json.dumps(event, sort_keys=True, default=str)
            except (TypeError, ValueError):
                material = repr(event)
            event_id = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

        with self._lock:
            if event_id in self._seen_ids:
                return {"status": "duplicate", "event_id": event_id}

            self._seen_ids[event_id] = None
            while len(self._seen_ids) > _MAX_SEEN_IDS:
                self._seen_ids.popitem(last=False)
            self._events.append(
                {
                    "event_id": event_id,
                    "ingest_ts": datetime.now(timezone.utc).isoformat(),
                    "ingest_time_source": "collector-node-clock",
                    "raw": event,
                }
            )
        return {
            "status": "accepted",
            "event_id": event_id,
            "ingest_time_source": "collector-node-clock",
        }


raw_collector = RawCollector()
