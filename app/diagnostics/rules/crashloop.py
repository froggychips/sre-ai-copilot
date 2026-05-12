"""CrashLoopBackOff detector."""
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


class CrashLoopBackOffRule(Rule):
    name = "CrashLoopBackOffRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)

        hits = self.count_matches(text, _CRASHLOOP_PATTERN)
        restart_matches = _RESTART_COUNT_PATTERN.findall(text)
        max_restarts = max((int(r) for r in restart_matches), default=0)

        if hits >= 1 or max_restarts >= 3:
            confidence = 0.9 if hits >= 1 else 0.7
            return [
                Fact(
                    kind=FactKind.CRASHLOOP,
                    observed=True,
                    confidence=confidence,
                    subject=ctx.get("pod") or ctx.get("service"),
                    evidence={
                        "phrase_hits": hits,
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
                evidence={"max_restart_count": max_restarts},
                source_rule=self.name,
            )
        ]
