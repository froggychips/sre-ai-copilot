"""G5+G2.2: confidence-score formula для edges.

Принимает (extras, last_seen_at) edge → returns [0, 1].

base = 0.5 (inferred-env baseline, что-либо без подтверждения)
× source multiplier (0/1/2/3+ источников)
× freshness multiplier (по last_seen_at)
clamp [0, 1]

Label thresholds:
  score ≥ 0.7 → "high"
  0.4 ≤ score < 0.7 → "medium"
  score < 0.4 → "low"

Используется в queries.upstream_of для дополнения dict-ответа и в
discord_service для badge в embed (●●●/●●○/●○○).

Будущие runtime-источники (OTEL / VM metrics) могут передавать
discovered_by="kg_sync/runtime_seen" — это добавит источник + поднимет
freshness, что даст конкретный edge ближе к "high".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def confidence_score(
    extras: Optional[Dict[str, Any]],
    last_seen_at: Optional[datetime],
) -> float:
    """Calculate edge confidence [0, 1] from discovery_sources + freshness."""
    sources = (extras or {}).get("discovery_sources") or []
    n = len(sources)
    if n == 0:
        # Backfill-эпохальные edges без provenance — нулевая уверенность.
        return 0.0

    if n == 1:
        src_mul = 1.0
    elif n == 2:
        src_mul = 1.3
    else:
        src_mul = 1.5

    if last_seen_at is None:
        fresh_mul = 0.5
    else:
        age_sec = (datetime.utcnow() - last_seen_at).total_seconds()
        age_days = age_sec / 86400.0
        if age_days < 1 / 24:        # < 1 час
            fresh_mul = 1.0
        elif age_days < 1:           # < 1 день
            fresh_mul = 0.95
        elif age_days < 7:           # < неделя
            fresh_mul = 0.8
        elif age_days < 30:          # < месяц
            fresh_mul = 0.5
        else:                        # > месяца — stale
            fresh_mul = 0.2

    base = 0.5
    return min(1.0, base * src_mul * fresh_mul)


def confidence_label(score: float) -> str:
    """Map [0, 1] score → human-readable bucket."""
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def confidence_badge(score: float) -> str:
    """Visual badge для Discord embed: ●●● / ●●○ / ●○○."""
    label = confidence_label(score)
    return {"high": "●●●", "medium": "●●○", "low": "●○○"}.get(label, "○○○")
