"""Upstream service degradation detector.

В отличие от прочих правил, этот опирается на enriched ctx["upstream_alerts"]
— список других alert-ов в окне ±N минут от текущего, на upstream-сервисах
(определяется по knowledge graph, см. слой B).

Три исхода:
  * `upstream_alerts is None` или источник помечен в source_status →
    UNKNOWN: граф не опрошен, сказать нечего. Раньше это был ✗ с
    confidence 0.3 — критик читал его как «upstream чист», хотя проверки
    не было;
  * пустой список → ABSENT 0.9: окно проверено, соседи молчат;
  * есть алерты → FOUND 0.85, OBSERVED: алерт — наблюдение, не вывод.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind, Verdict
from app.diagnostics.rules.base import Rule
from app.knowledge_graph.epistemic import Epistemic

_SOURCE = "upstream_alerts"
_PROVENANCE = "kg_alerts/upstream"


class UpstreamDegradedRule(Rule):
    name = "UpstreamDegradedRule"
    sources = (_SOURCE,)

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        subject = ctx.get("service")
        window_min = ctx.get("upstream_window_min")
        upstream_alerts = ctx.get(_SOURCE)

        problem = self.source_problem(ctx, _SOURCE)
        if upstream_alerts is None or problem:
            return [Fact.unknown(
                FactKind.UPSTREAM_DEGRADED, problem or "no_graph_data",
                subject=subject, source_rule=self.name,
                provenance=_PROVENANCE, window_min=window_min,
            )]

        if not upstream_alerts:
            return [Fact(
                kind=FactKind.UPSTREAM_DEGRADED,
                observed=False,
                confidence=0.9,
                subject=subject,
                evidence={"upstreams_checked": 0},
                source_rule=self.name,
                verdict=Verdict.ABSENT.value,
                epistemic=Epistemic.OBSERVED.value,
                provenance=_PROVENANCE,
                window_min=window_min,
            )]

        return [Fact(
            kind=FactKind.UPSTREAM_DEGRADED,
            observed=True,
            confidence=0.85,
            subject=subject,
            evidence={
                "count": len(upstream_alerts),
                "alerts": [
                    {
                        "service": a.get("service"),
                        "alertname": a.get("alertname"),
                        "minutes_before": a.get("minutes_before"),
                    }
                    for a in upstream_alerts
                ],
            },
            source_rule=self.name,
            verdict=Verdict.FOUND.value,
            epistemic=Epistemic.OBSERVED.value,
            provenance=_PROVENANCE,
            window_min=window_min,
        )]
