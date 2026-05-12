"""Resource pressure detector (CPU/memory headroom, node pressure)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

_PRESSURE_PATTERN = re.compile(
    r"(memorypressure|diskpressure|pidpressure|nodenotready|"
    r"high cpu usage|high memory usage|throttling)",
    re.IGNORECASE,
)


class ResourcePressureRule(Rule):
    name = "ResourcePressureRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)
        hits = self.count_matches(text, _PRESSURE_PATTERN)

        # Также смотрим numeric-метрики если есть.
        metrics = ctx.get("metrics_summary") or {}
        cpu_high = bool(metrics.get("cpu_pressure"))
        mem_high = bool(metrics.get("memory_pressure"))

        if hits == 0 and not cpu_high and not mem_high:
            return [
                Fact(
                    kind=FactKind.RESOURCE_PRESSURE,
                    observed=False,
                    confidence=0.75,
                    subject=ctx.get("namespace"),
                    source_rule=self.name,
                )
            ]

        types: List[str] = []
        if re.search(r"memorypressure|high memory|memory_pressure", text):
            types.append("memory")
        if re.search(r"diskpressure", text):
            types.append("disk")
        if re.search(r"pidpressure", text):
            types.append("pid")
        if re.search(r"high cpu|cpu_pressure|throttling", text) or cpu_high:
            types.append("cpu")
        if mem_high and "memory" not in types:
            types.append("memory")

        return [
            Fact(
                kind=FactKind.RESOURCE_PRESSURE,
                observed=True,
                confidence=0.8 if hits > 0 else 0.6,
                subject=ctx.get("namespace"),
                evidence={"types": sorted(set(types)) or ["unknown"], "hits": hits},
                source_rule=self.name,
            )
        ]
