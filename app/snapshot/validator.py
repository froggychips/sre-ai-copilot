from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.snapshot.schema import (
    IncidentSnapshotV1,
    compute_log_window_hash,
    compute_metric_snapshot_hash,
    compute_topology_hash,
)

# Допуск на рассинхрон часов между источником события (AlertManager пишет
# incident_ts своими часами) и коллектором снапшота (captured_at — часы нашей
# ноды). NTP-дрейф в пару секунд — норма, а строгое `incident_ts > captured_at`
# из-за него отправляло КАЖДЫЙ такой снапшот в DEGRADED/MEDIUM (и включало
# low_fidelity_mode в app/api/replay.py), т.е. сигнал снова обесценивался.
# Реально «будущие» снапшоты (минуты вперёд = подмена/битый источник) всё ещё
# ловятся: допуск сознательно меньше любого осмысленного окна инцидента.
_CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)

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


def _as_utc(dt: datetime) -> datetime:
    """Naive datetime трактуем как UTC — иначе aware/naive несравнимы."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    # Проверка согласованности только для СЕМАНТИЧЕСКИ упорядоченной пары:
    # incident_ts (момент инцидента) не может быть позже captured_at (момент
    # снятия снапшота). Раньше «монотоничность» проверялась по dict в порядке
    # вставки {"captured_at": now, "incident_ts": <прошлое>} — captured_at
    # всегда позже incident_ts, КАЖДЫЙ снапшот получал ложный warning и уходил
    # в DEGRADED/low-fidelity, сигнал был бесполезен. Мёртвая ветка
    # `min(...) > max(...)` (невозможна по определению min/max) удалена.
    incident_raw = timestamps.get("incident_ts")
    captured_raw = timestamps.get("captured_at")
    incident_dt = (
        _parse_iso(incident_raw)
        if isinstance(incident_raw, str) and incident_raw
        else None
    )
    captured_dt = (
        _parse_iso(captured_raw)
        if isinstance(captured_raw, str) and captured_raw
        else None
    )
    if incident_dt is not None and captured_dt is not None:
        # Допуск _CLOCK_SKEW_TOLERANCE: секунды дрейфа часов — не сигнал.
        if _as_utc(incident_dt) - _as_utc(captured_dt) > _CLOCK_SKEW_TOLERANCE:
            warnings.append(
                "incident_ts is after captured_at beyond clock-skew tolerance "
                f"({int(_CLOCK_SKEW_TOLERANCE.total_seconds())}s) — "
                "clock skew or bad source timestamp"
            )

    payload = snapshot.get("payload") or {}
    # Все три хэша пересчитываем по payload теми же формулами, что и при
    # создании снапшота (app/snapshot/schema.py). topology_hash раньше НЕ
    # перепроверялся, хотя материал (payload.targets + policy_name) лежит
    # рядом — подменённая/разъехавшаяся топология в снапшоте проходила
    # валидацию как PASS и уезжала в replay.
    if snapshot.get("topology_hash") != compute_topology_hash(payload):
        errors.append("topology_hash mismatch")
    if snapshot.get("metric_snapshot_hash") != compute_metric_snapshot_hash(payload):
        errors.append("metric_snapshot_hash mismatch")
    if snapshot.get("log_window_hash") != compute_log_window_hash(payload):
        errors.append("log_window_hash mismatch")

    correlation_index = snapshot.get("correlation_index") or []
    source_set = set(str(i) for i in source_event_ids)
    for i, rel in enumerate(correlation_index):
        event_id = str(rel.get("event_id", ""))
        related_to = [str(x) for x in rel.get("related_to", [])]
        if event_id not in source_set:
            errors.append(f"correlation_index[{i}] invalid event_id")
        if not set(related_to).issubset(source_set):
            errors.append(
                f"correlation_index[{i}] related_to must be subset of source_event_ids"
            )

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
