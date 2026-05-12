"""Fan-out hypothesis-стадия из 3 perspective-агентов + anchor-валидация.

Архитектурный смысл (см. обсуждение в чате):
    * Один hypothesis-агент с одним промптом = single shared prior →
      mode collapse.
    * Три perspective-агента (app / infra / deps) с разными ролями +
      обязанностью anchor-ить каждую гипотезу к конкретным fact_kind
      из FactStore → реальная диверсификация рассуждения.
    * После fan-out отбрасываем гипотезы, чьи anchor-факты НЕ
      observed в FactStore. Это режет «свободные рассуждения» LLM.
    * Disagreement между перспективами — сигнал, не баг (см.
      HypothesisSet.disagreement_signal).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import structlog
from pydantic import ValidationError

from app.agents.base import BaseAgent
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.diagnostics.facts import FactKind, FactStore
from app.observability.ai_metrics import track_generated, track_grounded
from app.services.telemetry_utils import trace_agent

logger = structlog.get_logger()


# Каноничные perspective-роли. Расширять — только синхронно с тестами.
PERSPECTIVES: Dict[str, str] = {
    "app": (
        "SRE focused on the APPLICATION layer: code changes, config rollouts, "
        "feature flags, framework-specific failure modes (panic, exception, "
        "config parsing), business logic regressions."
    ),
    "infra": (
        "SRE focused on the INFRASTRUCTURE layer: pod scheduling, node "
        "health, CPU/memory pressure, OOM, eviction, kubelet/CRI issues, "
        "storage and PV problems."
    ),
    "deps": (
        "SRE focused on DEPENDENCIES and TRAFFIC: upstream services, "
        "network partitions, DNS, TLS, latency cascades, load balancer "
        "failover, queue backpressure."
    ),
    "runtime": (
        "SRE focused on APPLICATION RUNTIME crashes: process_crash facts "
        "(SIGSEGV/SIGABRT/SIGILL exit codes 139/134/132), .NET CLR / JVM / Go "
        "runtime failures, native P/Invoke interop bugs, heap/stack corruption, "
        "unhandled exceptions in async code, GC-induced pauses, thread pool "
        "exhaustion, finalizer panics. When process_crash is observed — this is "
        "your primary perspective. Look for runtime-specific signals: exit codes, "
        "core dumps, signal names in logs."
    ),
}


def _facts_block(facts: FactStore) -> str:
    """Маркап фактов для prompt-а. Точный формат — обязанность FactStore."""
    return facts.to_prompt_context()


def _allowed_kinds_block() -> str:
    return ", ".join(sorted(FactKind.ALL))


def _prompt_for_perspective(
    perspective: str,
    incident_summary: str,
    facts: FactStore,
) -> tuple[str, str]:
    """(user_context, instruction) для одного perspective-агента."""
    user_context = (
        f"<incident_summary>\n{incident_summary}\n</incident_summary>\n\n"
        f"{_facts_block(facts)}\n\n"
        f"<allowed_anchors>\n{_allowed_kinds_block()}\n</allowed_anchors>"
    )
    role_description = PERSPECTIVES[perspective]
    instruction = (
        f"You are a {role_description}\n\n"
        "Produce up to 3 hypotheses about the root cause of this incident, "
        "from YOUR perspective only. Hard rules:\n"
        "  1. Every hypothesis MUST reference at least one fact_kind from "
        "<allowed_anchors>, AND that fact must be marked ✓ (observed) in "
        "<facts>. Do not invent kinds; do not anchor to ✗ facts.\n"
        "  2. If <facts> gives you nothing observed from your perspective, "
        "return an empty list. Honesty > making things up.\n"
        "  3. Output VALID JSON ONLY, exactly this shape:\n"
        "     {\"hypotheses\":[\n"
        "       {\"cause\": \"...\", \"detail\": \"...\", "
        "\"anchored_facts\": [\"oom_killed\", ...], "
        "\"confidence\": 0.0-1.0},\n"
        "       ...\n"
        "     ]}\n"
        "  4. No prose outside the JSON. No markdown fences."
    )
    return user_context, instruction


class PerspectiveAgent(BaseAgent):
    """LLM-агент с фиксированной perspective-ролью.

    Контракт: возвращает list[Hypothesis], НЕ raw-текст. Парсинг и
    валидация — здесь, чтобы оркестратор не возился со строками.
    """

    def __init__(self, perspective: str):
        if perspective not in PERSPECTIVES:
            raise ValueError(f"unknown perspective: {perspective}")
        super().__init__(
            name=f"Hypothesis-{perspective}",
            role=PERSPECTIVES[perspective],
            task_type="hypothesis",
        )
        self.perspective = perspective

    async def generate(
        self,
        incident_summary: str,
        facts: FactStore,
    ) -> List[Hypothesis]:
        user_context, instruction = _prompt_for_perspective(
            self.perspective, incident_summary, facts
        )
        raw = await self.ask(user_context=user_context, instruction=instruction)
        return _parse_hypotheses(raw, perspective=self.perspective)


def _parse_hypotheses(raw: str, perspective: str) -> List[Hypothesis]:
    """LLM-output → list[Hypothesis]. Robust к мусору."""
    if not raw or not raw.strip():
        return []
    # Иногда LLM оборачивает JSON в ```json ... ``` несмотря на инструкцию.
    s = raw.strip()
    if s.startswith("```"):
        # вырезаем первую и последнюю ``` строку
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))

    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        logger.warning(
            "hypothesis_parse_failed",
            perspective=perspective,
            error=str(e),
            raw_head=raw[:200],
        )
        return []

    items_raw = data.get("hypotheses") if isinstance(data, dict) else None
    if not isinstance(items_raw, list):
        logger.warning(
            "hypothesis_parse_no_list", perspective=perspective, got=type(data).__name__
        )
        return []

    out: List[Hypothesis] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        item = {**item, "perspective": perspective}
        item.setdefault("detail", "")
        try:
            out.append(Hypothesis(**item))
        except ValidationError as e:
            logger.warning(
                "hypothesis_invalid",
                perspective=perspective,
                error=e.errors(),
                item=item,
            )
            continue
    return out


class MultiHypothesisAgent:
    """Оркестратор fan-out стадии.

    Не наследует BaseAgent — он сам по себе LLM не дёргает, только
    параллелит PerspectiveAgent.generate(). Это упрощает тестирование:
    мокать можно отдельные perspective-агенты.
    """

    def __init__(self, perspectives: Optional[List[str]] = None) -> None:
        self.perspectives = perspectives or list(PERSPECTIVES.keys())
        self._agents = [PerspectiveAgent(p) for p in self.perspectives]

    @trace_agent("MultiHypothesis")
    async def generate(
        self, incident_summary: str, facts: FactStore
    ) -> HypothesisSet:
        # Параллельный fan-out. Если один perspective упадёт — остальные
        # вернутся, и мы продолжим. Это критично для устойчивости
        # pipeline-а к flaky LLM-провайдеру.
        tasks = [
            agent.generate(incident_summary, facts) for agent in self._agents
        ]
        results: List[Any] = await asyncio.gather(*tasks, return_exceptions=True)

        merged: List[Hypothesis] = []
        for agent, res in zip(self._agents, results):
            if isinstance(res, Exception):
                logger.warning(
                    "perspective_failed",
                    perspective=agent.perspective,
                    error=type(res).__name__,
                )
                continue
            merged.extend(res)

        unfiltered = HypothesisSet(items=merged)
        for h in unfiltered.items:
            track_generated(perspective=h.perspective)
        grounded = unfiltered.filter_grounded(facts.observed_kinds())

        # Каждая выжившая гипотеза → +1 в метрику по своей perspective.
        # Это даёт картину «какая perspective реально что-то выдаёт на
        # нашем потоке alert-ов».
        for h in grounded.items:
            track_grounded(perspective=h.perspective)

        logger.info(
            "multi_hypothesis.fanout_done",
            raw_count=len(unfiltered.items),
            grounded_count=len(grounded.items),
            disagreement=grounded.disagreement_signal(),
            consensus_kinds=grounded.consensus_kinds(),
        )
        return grounded
