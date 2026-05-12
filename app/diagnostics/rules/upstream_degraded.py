"""Upstream service degradation detector.

В отличие от прочих правил, этот опирается на enriched ctx["upstream_alerts"]
— список других alert-ов в окне ±N минут от текущего, на upstream-сервисах
(определяется по knowledge graph, см. слой B). Если граф ещё не наполнен,
правило отдаёт observed=False с reason=no_graph.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule


class UpstreamDegradedRule(Rule):
    name = "UpstreamDegradedRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        upstream_alerts = ctx.get("upstream_alerts")
        if upstream_alerts is None:
            return [
                Fact(
                    kind=FactKind.UPSTREAM_DEGRADED,
                    observed=False,
                    confidence=0.3,  # низкий — мы просто не смогли проверить
                    subject=ctx.get("service"),
                    evidence={"reason": "no_graph_data"},
                    source_rule=self.name,
                )
            ]

        if not upstream_alerts:
            return [
                Fact(
                    kind=FactKind.UPSTREAM_DEGRADED,
                    observed=False,
                    confidence=0.9,
                    subject=ctx.get("service"),
                    evidence={"upstreams_checked": 0},
                    source_rule=self.name,
                )
            ]

        return [
            Fact(
                kind=FactKind.UPSTREAM_DEGRADED,
                observed=True,
                confidence=0.85,
                subject=ctx.get("service"),
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
            )
        ]
