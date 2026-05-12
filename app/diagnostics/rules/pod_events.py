"""PodEventsRule — факты из k8s Events.

Читает ctx["k8s_events"] (список dict-ов от K8sFacts.collect_snapshot)
и маппит event reason → FactKind. В отличие от regex-правил, source —
структурированные данные k8s API, поэтому confidence выше.

Маппинг event reason → FactKind:
    OOMKilling, OOMKilled        → oom_killed       (0.95)
    FailedScheduling             → failed_scheduling (0.95)
    Evicted                      → resource_pressure (0.90)
    BackOff, CrashLoopBackOff    → crashloop         (0.85)
    MemoryPressure, DiskPressure → resource_pressure (0.85)

Правило намеренно не дублирует ✗-сигналы: отсутствие события — не
доказательство отсутствия факта. ✗ выдают профильные правила (OOMKilledRule
и т.д.), если они не нашли своих источников.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

# (reason_lower_prefix, fact_kind, confidence)
_REASON_MAP: List[Tuple[str, str, float]] = [
    ("oomkill",          FactKind.OOM_KILLED,        0.95),
    ("evict",            FactKind.RESOURCE_PRESSURE,  0.90),
    ("memorypressure",   FactKind.RESOURCE_PRESSURE,  0.85),
    ("diskpressure",     FactKind.RESOURCE_PRESSURE,  0.85),
    ("failedscheduling", FactKind.FAILED_SCHEDULING,  0.95),
    ("crashloopbackoff", FactKind.CRASHLOOP,          0.85),
    ("backoff",          FactKind.CRASHLOOP,          0.75),
]


def _match_reason(reason: str) -> Tuple[str, float] | None:
    r = reason.lower()
    for prefix, kind, conf in _REASON_MAP:
        if r.startswith(prefix) or prefix in r:
            return kind, conf
    return None


class PodEventsRule(Rule):
    name = "PodEventsRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        events = ctx.get("k8s_events") or []
        if not events:
            return []

        # Агрегируем по kind: берём максимальную confidence среди всех
        # событий этого kind. Один OOMKilling-ивент достаточен.
        best: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for ev in events:
            match = _match_reason(ev.get("reason", ""))
            if match is None:
                continue
            kind, conf = match
            if kind not in best or conf > best[kind][0]:
                best[kind] = (
                    conf,
                    {
                        "source": "k8s_event",
                        "reason": ev["reason"],
                        "message": ev.get("message", "")[:120],
                        "count": ev.get("count", 1),
                    },
                )

        subject = ctx.get("pod") or ctx.get("service") or ctx.get("namespace")
        return [
            Fact(
                kind=kind,
                observed=True,
                confidence=conf,
                subject=subject,
                evidence=evidence,
                source_rule=self.name,
            )
            for kind, (conf, evidence) in best.items()
        ]
