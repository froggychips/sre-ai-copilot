"""DiagnosticEngine — оркестратор правил.

Принципы:
  * Правила НЕЗАВИСИМЫ. Падение одного не блокирует остальные.
  * Результат — FactStore (read-only снаружи), который дальше уходит в
    hypothesis-агенты как enriched context.
  * Выполнение синхронное и быстрое (без LLM, без сети). Дорогой
    enrichment делается ДО этого слоя (k8s_facts.collect и т.п.).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from app.diagnostics.facts import Fact, FactStore
from app.diagnostics.rules import DEFAULT_RULES, Rule

logger = structlog.get_logger()


class DiagnosticEngine:
    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self.rules: List[Rule] = list(rules) if rules is not None else list(DEFAULT_RULES)

    def run(self, ctx: Dict[str, Any]) -> FactStore:
        store = FactStore()
        for rule in self.rules:
            try:
                facts = rule.evaluate(ctx)
            except Exception as e:
                logger.error(
                    "diagnostic_rule_failed",
                    rule=rule.name,
                    error=type(e).__name__,
                    message=str(e),
                )
                continue
            if not facts:
                continue
            for f in facts:
                if not isinstance(f, Fact):
                    logger.warning(
                        "diagnostic_rule_returned_non_fact",
                        rule=rule.name,
                        got=type(f).__name__,
                    )
                    continue
                store.add(f)
        return store


# Singleton для случаев, когда нет смысла переопределять правила.
default_engine = DiagnosticEngine()
