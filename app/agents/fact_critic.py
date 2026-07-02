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


# ── Пороги алгоритмической оценки confidence anchor-факта ────────────────
# Раньше здесь был единый жёсткий порог <0.5: любой observed anchor слабее
# него получал refutation и ГИПОТЕЗА УМИРАЛА до LLM. Проблема в том, что
# легитимные детерминированные правила штатно эмитят observed-факты ниже 0.5:
#   * soft-OOM (только `exit 137`, без явного OOMKilled) — conf=0.40 (oom.py)
#   * generic-crash regex fallback (неизвестный ненулевой exit) — conf=0.45
#     (process_crash.py)
# Когда такой факт — ЕДИНСТВЕННЫЙ anchor, старый порог опровергал все гипотезы
# → best_candidate=None → TRIAGE_REQUIRED без предложенного фикса ровно на тех
# слабосигнальных инцидентах, ради которых multi-hypothesis движок и задуман.
#
# Теперь поведение градуированное:
#   confidence < _VERY_LOW_CONFIDENCE            → жёсткий algo-refutation
#                                                  (сигнал пренебрежимо мал);
#   _VERY_LOW_CONFIDENCE ≤ conf < _LOW_CONFIDENCE → НЕ опровергаем, а мягко
#                                                  снижаем confidence гипотезы
#                                                  (она доживает до synthesis,
#                                                   но ранжируется ниже).
_VERY_LOW_CONFIDENCE = 0.25
_LOW_CONFIDENCE = 0.5
# Множитель штрафа для «слабых-но-observed» anchor-ов (диапазон soft-зоны).
# 0.6 достаточно, чтобы уверенный конкурент (conf≥0.5) обошёл слабую гипотезу
# в best_candidate, но недостаточно, чтобы обнулить её как кандидата.
_LOW_CONFIDENCE_PENALTY = 0.6


def _observed_anchor_confidences(h: Hypothesis, facts: FactStore) -> dict:
    """kind → максимальная confidence среди его observed-фактов.

    Только для anchor-ов гипотезы, которые реально observed. Не-observed
    anchor-ы сюда не попадают (их ловит refutation 'NOT observed')."""
    observed = facts.observed_kinds()
    out: dict = {}
    for kind in h.anchored_facts:
        if kind not in observed:
            continue
        relevant = [f for f in facts.by_kind(kind) if f.observed]
        if relevant:
            out[kind] = max(f.confidence for f in relevant)
    return out


def _algorithmic_refutations(h: Hypothesis, facts: FactStore) -> List[str]:
    """Дешёвая алгоритмическая проверка ДО LLM — ТОЛЬКО жёсткие опровержения.

    Возвращает refutation-строки лишь там, где сигнал объективно отсутствует
    или пренебрежимо мал:
      1. anchor-факт не observed (для гипотезы, прошедшей filter_grounded —
         не должно случаться, но дешёвая страховка).
      2. anchor observed, но с ОЧЕНЬ низкой confidence (< _VERY_LOW_CONFIDENCE).

    ВАЖНО: observed-факты в диапазоне [_VERY_LOW_CONFIDENCE, _LOW_CONFIDENCE)
    здесь НЕ опровергаются — это легитимный диапазон детерминированных правил
    (soft-OOM=0.40, generic-crash=0.45). Их обрабатывает мягкий down-weight
    в _algorithmic_confidence_penalty, чтобы не убивать единственный anchor.
    """
    out: List[str] = []
    observed = facts.observed_kinds()
    for kind in h.anchored_facts:
        if kind not in observed:
            out.append(f"algo: anchor '{kind}' is NOT observed in fact store")
            continue
        relevant = [f for f in facts.by_kind(kind) if f.observed]
        if relevant and max(f.confidence for f in relevant) < _VERY_LOW_CONFIDENCE:
            out.append(
                f"algo: anchor '{kind}' is observed only with low confidence "
                f"({max(f.confidence for f in relevant):.2f})"
            )
    return out


def _algorithmic_confidence_penalty(h: Hypothesis, facts: FactStore) -> float:
    """Множитель мягкого down-weight-а confidence для «слабых-но-observed» anchor-ов.

    Возвращает значение в (0, 1]: 1.0 = штрафа нет. Штраф применяется, если
    среди observed anchor-ов есть хотя бы один в soft-зоне
    [_VERY_LOW_CONFIDENCE, _LOW_CONFIDENCE). Это НЕ refutation: гипотеза
    остаётся живой (survivors её видит), но её confidence падает, поэтому в
    best_candidate/synthesis она честно уступает более сильным кандидатам.
    """
    for conf in _observed_anchor_confidences(h, facts).values():
        if _VERY_LOW_CONFIDENCE <= conf < _LOW_CONFIDENCE:
            return _LOW_CONFIDENCE_PENALTY
    return 1.0


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

        # Мягкий down-weight для «слабых-но-observed» anchor-ов (soft-зона).
        # Гипотеза НЕ опровергается (survivors её пропустит), но confidence
        # снижается ДО прогона LLM, чтобы уверенные конкуренты честно её
        # обошли в best_candidate/synthesis. Grounding и LLM-refutation-логику
        # это не трогает — только ранжирование единственного слабого сигнала.
        working = hypothesis
        penalty = _algorithmic_confidence_penalty(hypothesis, facts)
        if penalty < 1.0:
            working = hypothesis.model_copy(
                update={"confidence": round(hypothesis.confidence * penalty, 4)}
            )
            logger.debug(
                "critic_soft_downweight",
                hypothesis_cause=hypothesis.cause,
                from_confidence=hypothesis.confidence,
                to_confidence=working.confidence,
            )

        user_context, instruction = _llm_refutation_prompt(working, facts)
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
            # без LLM — критик молчит, не ставит ложные refutations;
            # но применённый down-weight сохраняем (это не refutation).
            return working
        track_stage_duration("llm_critic", time.monotonic() - _t0)

        llm_refs = _parse_refutations(raw)
        if llm_refs:
            track_refuted("llm")
        return working.model_copy(update={"refutations": llm_refs})

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
