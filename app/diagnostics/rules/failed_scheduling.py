"""FailedScheduling detector (Insufficient cpu/memory, taint mismatch, ...)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

_SCHEDULING_PATTERN = re.compile(
    r"(failedscheduling|0/\d+ nodes are available|insufficient (?:cpu|memory)|"
    r"taint.*?untolerated|didn'?t match.*?affinity|untolerated taint)",
    re.IGNORECASE,
)


class FailedSchedulingRule(Rule):
    name = "FailedSchedulingRule"
    sources = ("k8s_summary", "logs_summary")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)
        hits = self.count_matches(text, _SCHEDULING_PATTERN)
        if hits == 0:
            return [
                Fact(
                    kind=FactKind.FAILED_SCHEDULING,
                    observed=False,
                    confidence=0.85,
                    subject=ctx.get("namespace"),
                    source_rule=self.name,
                )
            ]

        # Извлекаем cause более точно, если можем.
        cause = "unknown"
        if re.search(r"insufficient cpu", text):
            cause = "insufficient_cpu"
        elif re.search(r"insufficient memory", text):
            cause = "insufficient_memory"
        elif re.search(r"taint", text):
            cause = "taint_mismatch"
        elif re.search(r"affinity", text):
            cause = "affinity_mismatch"

        return [
            Fact(
                kind=FactKind.FAILED_SCHEDULING,
                observed=True,
                confidence=0.9,
                subject=ctx.get("namespace"),
                evidence={"hits": hits, "cause": cause},
                source_rule=self.name,
            )
        ]
