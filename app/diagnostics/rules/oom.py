"""OOMKilled detector.

Ищет в текстовом enriched context признаки OOM-событий: alertname,
k8s events summary, описание alert-а. Конкретные паттерны взяты из
реальных текстов k8s events:
    "OOMKilled"
    "out of memory"
    "Container ... was OOM killed"
    "exit code 137"          # SIGKILL, чаще всего OOM, но не всегда
    "memory cgroup out of memory"
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

# Сильные индикаторы — high-confidence.
_HARD_PATTERN = re.compile(
    r"(oom\s?killed|out of memory|memory cgroup out of memory|killed.*?oom)",
    re.IGNORECASE,
)
# Слабый: exit 137 может быть от ручного kill -9, не только OOM.
_SOFT_PATTERN = re.compile(r"exit\s*code\s*137|\bsigkill\b", re.IGNORECASE)


class OOMKilledRule(Rule):
    name = "OOMKilledRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)

        hard = self.count_matches(text, _HARD_PATTERN)
        soft = self.count_matches(text, _SOFT_PATTERN)

        if hard >= 1:
            return [
                Fact(
                    kind=FactKind.OOM_KILLED,
                    observed=True,
                    confidence=0.95,
                    subject=ctx.get("pod") or ctx.get("service"),
                    evidence={
                        "hard_matches": hard,
                        "soft_matches": soft,
                    },
                    source_rule=self.name,
                )
            ]
        if soft >= 1:
            # exit 137 без явного OOMKilled — слабый сигнал. Фиксируем как
            # observed=True но с низкой confidence; критик потом может
            # отбраковать гипотезу, которая на него опирается.
            return [
                Fact(
                    kind=FactKind.OOM_KILLED,
                    observed=True,
                    confidence=0.4,
                    subject=ctx.get("pod") or ctx.get("service"),
                    evidence={"soft_matches": soft, "note": "exit 137 only"},
                    source_rule=self.name,
                )
            ]
        return [
            Fact(
                kind=FactKind.OOM_KILLED,
                observed=False,
                confidence=0.9,
                subject=ctx.get("pod") or ctx.get("service"),
                evidence={},
                source_rule=self.name,
            )
        ]
