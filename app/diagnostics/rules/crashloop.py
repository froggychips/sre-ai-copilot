"""CrashLoopBackOff detector.

Активный crashloop детектится по СВЕЖИМ сигналам: фразы в наблюдаемом тексте
(alert description / k8s snapshot) и recent BackOff-events из k8s_events.
Кумулятивный `restart_count` сам по себе сигналом НЕ является: под с 5
рестартами за месяцы жизни — это история, а не активный crashloop (раньше он
давал observed=True conf 0.7). Счётчик сохраняем в evidence для отладки.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

_CRASHLOOP_PATTERN = re.compile(
    r"(crashloopbackoff|back-off restarting failed container|liveness probe failed)",
    re.IGNORECASE,
)
_RESTART_COUNT_PATTERN = re.compile(
    r"restart(?:s|\s*count)?\s*[:=]\s*(\d+)", re.IGNORECASE
)

# Event reasons, указывающие на активный restart-цикл. k8s_events отсортированы
# по last_timestamp DESC и собираются в окне инцидента — это «recent» сигнал.
_BACKOFF_EVENT_REASONS = frozenset({"BackOff", "CrashLoopBackOff"})


def _recent_backoff_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Отфильтровать события активного restart-цикла из ctx["k8s_events"]."""
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        reason = e.get("reason") or ""
        message = (e.get("message") or "").lower()
        if reason in _BACKOFF_EVENT_REASONS or "back-off restarting" in message:
            out.append(e)
    return out


class CrashLoopBackOffRule(Rule):
    name = "CrashLoopBackOffRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)

        hits = self.count_matches(text, _CRASHLOOP_PATTERN)
        restart_matches = _RESTART_COUNT_PATTERN.findall(text)
        max_restarts = max((int(r) for r in restart_matches), default=0)
        backoff_events = _recent_backoff_events(ctx.get("k8s_events") or [])

        if hits >= 1 or backoff_events:
            # Фраза в наблюдаемом тексте — сильный сигнал; свежие
            # BackOff-events — чуть слабее (могут быть от initial-старта).
            confidence = 0.9 if hits >= 1 else 0.8
            return [
                Fact(
                    kind=FactKind.CRASHLOOP,
                    observed=True,
                    confidence=confidence,
                    subject=ctx.get("pod") or ctx.get("service"),
                    evidence={
                        "phrase_hits": hits,
                        "recent_backoff_events": len(backoff_events),
                        "max_restart_count": max_restarts,
                    },
                    source_rule=self.name,
                )
            ]
        return [
            Fact(
                kind=FactKind.CRASHLOOP,
                observed=False,
                confidence=0.85,
                subject=ctx.get("pod") or ctx.get("service"),
                evidence={
                    # Кумулятивный счётчик — только справка: без свежего
                    # BackOff-сигнала он не доказывает активный crashloop.
                    "max_restart_count": max_restarts,
                },
                source_rule=self.name,
            )
        ]
