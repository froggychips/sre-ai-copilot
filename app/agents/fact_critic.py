"""Fact-based adversarial critic.

Принципиальное отличие от старого CriticAgent: задача не «оценить
правдоподобность гипотезы», а «найти конкретный факт, который её
ОПРОВЕРГАЕТ». Это переводит критика из judge-mode (где он может
бесцельно соглашаться с hypothesis) в adversarial-mode (где он
получает балл только за найденные противоречия).

Подход:
    Hypothesis: "service degraded из-за recent_deploy"
    Facts:     [recent_deploy=✗, oom_killed=✓, upstream_degraded=✗]
        ↓
    Critic ищет противоречия:
        * anchored_facts включают "recent_deploy", но в Facts он ✗
            → refutation: "anchor recent_deploy is NOT observed"
        * other observed facts (oom_killed) не упомянуты в гипотезе,
          но напрямую относятся к симптому → soft signal
        ↓
    Возврат:
        Hypothesis с заполненным refutations[].

Лёгкая первая проверка делается алгоритмически (anchor vs observed),
тяжёлая семантическая — через LLM-промпт «найди контрпример». Это
дешевле и точнее, чем гонять LLM на простые случаи.
"""
from __future__ import annotations

import json
import time
from typing import List, Optional

import structlog

from app.agents.base import BaseAgent
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.diagnostics.facts import FactStore
from app.observability.ai_metrics import track_refuted, track_stage_duration
from app.services.telemetry_utils import trace_agent

logger = structlog.get_logger()


def _algorithmic_refutations(h: Hypothesis, facts: FactStore) -> List[str]:
    """Дешёвая алгоритмическая проверка ДО LLM.

    Ищет очевидные противоречия без вызова модели:
      1. anchor-факт не observed (для гипотезы, прошедшей filter_grounded —
         не должно случаться, но дешёвая страховка).
      2. anchor confidence < 0.5 — слабый сигнал, не должен быть единственным.
    """
    out: List[str] = []
    observed = facts.observed_kinds()
    for kind in h.anchored_facts:
        if kind not in observed:
            out.append(f"algo: anchor '{kind}' is NOT observed in fact store")
            continue
        relevant = [f for f in facts.by_kind(kind) if f.observed]
        if relevant and max(f.confidence for f in relevant) < 0.5:
            out.append(
                f"algo: anchor '{kind}' is observed only with low confidence "
                f"({max(f.confidence for f in relevant):.2f})"
            )
    return out


def _llm_refutation_prompt(
    h: Hypothesis, facts: FactStore
) -> tuple[str, str]:
    """(user_context, instruction) для LLM-критика."""
    user_context = (
        f"<hypothesis>\n"
        f"  cause: {h.cause}\n"
        f"  detail: {h.detail}\n"
        f"  anchored_facts: {h.anchored_facts}\n"
        f"  confidence: {h.confidence}\n"
        f"  perspective: {h.perspective}\n"
        f"</hypothesis>\n\n"
        f"{facts.to_prompt_context()}"
    )
    instruction = (
        "You are an ADVERSARIAL critic. Your job is NOT to evaluate the "
        "hypothesis on a likability scale, but to find concrete facts that "
        "REFUTE it (counter-examples).\n\n"
        "Rules:\n"
        "  1. A refutation MUST point to a specific fact_kind in <facts>. "
        "Quote the fact_kind verbatim.\n"
        "  2. If a fact is marked ✗ (NOT observed), and the hypothesis "
        "needs it to be true, that's a refutation.\n"
        "  3. If an observed fact directly contradicts the cause "
        "(e.g. recent_deploy observed but hypothesis blames hardware), "
        "that's a refutation.\n"
        "  4. If you find NO refutations after honest analysis, return an "
        "empty list. Do not invent weaknesses.\n\n"
        "Output VALID JSON ONLY:\n"
        "  {\"refutations\": [\"text mentioning one fact_kind\", ...]}\n"
        "No prose, no markdown fences."
    )
    return user_context, instruction


def _parse_refutations(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        logger.warning("critic_parse_failed", raw_head=raw[:200])
        return []
    refs = data.get("refutations") if isinstance(data, dict) else None
    if not isinstance(refs, list):
        return []
    return [str(r) for r in refs if r]


class FactCriticAgent(BaseAgent):
    """LLM-критик, работающий поверх anchor-структуры.

    Не принимает свободные тексты hypothesis — только структурный
    Hypothesis + FactStore. Иначе он быстро уплывает обратно в judge-mode.
    """

    def __init__(self) -> None:
        super().__init__(
            name="FactCritic",
            role=(
                "Adversarial fact-checker. Find facts that refute the "
                "given hypothesis. Never judge persuasiveness, only "
                "evidence consistency."
            ),
            task_type="critic",
        )

    @trace_agent("FactCritic")
    async def critique(
        self, hypothesis: Hypothesis, facts: FactStore
    ) -> Hypothesis:
        algo = _algorithmic_refutations(hypothesis, facts)
        if algo:
            # Если уже на дешёвой проверке валится — LLM не дёргаем,
            # это экономит токены и даёт стабильный воспроизводимый
            # refutation для обвалившихся anchor-ов.
            track_refuted("algo")
            return hypothesis.model_copy(update={"refutations": algo})

        user_context, instruction = _llm_refutation_prompt(hypothesis, facts)
        _t0 = time.monotonic()
        try:
            raw = await self.ask(user_context=user_context, instruction=instruction)
        except Exception as e:
            track_stage_duration("llm_critic", time.monotonic() - _t0)
            logger.warning(
                "critic_llm_failed",
                error=type(e).__name__,
                hypothesis_cause=hypothesis.cause,
            )
            return hypothesis  # без LLM — критик молчит, не ставит ложные refutations
        track_stage_duration("llm_critic", time.monotonic() - _t0)

        llm_refs = _parse_refutations(raw)
        if llm_refs:
            track_refuted("llm")
        return hypothesis.model_copy(update={"refutations": llm_refs})

    async def critique_all(
        self, hyp_set: HypothesisSet, facts: FactStore
    ) -> HypothesisSet:
        """Прогон всех гипотез из set-а. Последовательно — параллелить
        не нужно, у каждой гипотезы свой кэш в LLM-провайдере не сработает,
        а скачок 3× нагрузки на API вреднее, чем доп. латентность."""
        critiqued: List[Hypothesis] = []
        for h in hyp_set.items:
            critiqued.append(await self.critique(h, facts))
        return HypothesisSet(items=critiqued)


def survivors(hyp_set: HypothesisSet) -> HypothesisSet:
    """Гипотезы без refutations — выходят в synthesis-стадию."""
    return HypothesisSet(items=[h for h in hyp_set.items if not h.refutations])


def refuted(hyp_set: HypothesisSet) -> HypothesisSet:
    """Гипотезы с refutations — отбракованные. Сохраняем для отчёта."""
    return HypothesisSet(items=[h for h in hyp_set.items if h.refutations])


def best_candidate(hyp_set: HypothesisSet) -> Optional[Hypothesis]:
    """Из выживших — самая уверенная. Tie-breaker: больше anchor-ов."""
    surv = survivors(hyp_set).items
    if not surv:
        return None
    return max(surv, key=lambda h: (h.confidence, len(h.anchored_facts)))
