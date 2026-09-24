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

Два режима LLM-части (`FACT_CRITIC_MODE`):
    per_hypothesis (default) — вызов на гипотезу. Замер 24.09.2026 на
        claude_cli: ~11 из ~16 вызовов кейса и основная латентность.
    batch — все гипотезы, пережившие algo-проверку, одним вызовом с
        ответом по id. Гипотеза, по которой ответа нет, считается НЕ
        проверенной и получает refutation — молчание модели не
        превращается в «противоречий не найдено».
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

import structlog

from app.agents.base import BaseAgent
from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.config import settings
from app.diagnostics.facts import FactStore
from app.observability.ai_metrics import track_refuted, track_stage_duration
from app.services.llm_service import LLMTruncatedResponse
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
    unknown = facts.unknown_kinds()
    for kind in h.anchored_facts:
        if kind not in observed:
            # Evidence-контракт: ? — не ✗. Если проверку выполнить не удалось
            # (источник упал, сервиса нет в графе), отсутствие факта ничего
            # не опровергает — гипотеза остаётся непроверенной, не ложной.
            if kind in unknown:
                continue
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
        "  2. If a fact is marked ✗ (checked and NOT observed), and the "
        "hypothesis needs it to be true, that's a refutation. A fact marked "
        "? (unknown: the check could not be performed) is NOT a refutation "
        "— never cite a ? fact as evidence against a hypothesis.\n"
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


# ── batch-режим ──────────────────────────────────────────────────────────

# Refutation для гипотезы, по которой пакетный ответ вердикта не дал. Это
# НЕ «критик не нашёл противоречий»: модель пропустила id, ответила не тем
# форматом или JSON не разобрался. Пропустить такую гипотезу в survivors
# значило бы выдать непроверенное за проверенное — ровно то, от чего
# adversarial-критик и защищает.
BATCH_NO_VERDICT = "critic: no verdict for this hypothesis in batch response"
# Гипотеза за пределами FACT_CRITIC_BATCH_TOP_N: LLM её не видела. Та же
# логика — не проверена, значит не выживает.
BATCH_NOT_REVIEWED = "critic: not reviewed (outside batch top-N by confidence)"


def _batch_prompt(
    items: List[Tuple[str, Hypothesis]], facts: FactStore
) -> tuple[str, str]:
    """(user_context, instruction) для пакетного критика.

    Факты — один раз на весь пакет: они общие для всех гипотез, и именно
    их повтор делал per-hypothesis режим дорогим по входу.
    """
    blocks = []
    for hid, h in items:
        blocks.append(
            f'<hypothesis id="{hid}">\n'
            f"  cause: {h.cause}\n"
            f"  detail: {h.detail}\n"
            f"  anchored_facts: {h.anchored_facts}\n"
            f"  confidence: {h.confidence}\n"
            f"  perspective: {h.perspective}\n"
            f"</hypothesis>"
        )
    user_context = (
        "<hypotheses>\n" + "\n".join(blocks) + "\n</hypotheses>\n\n"
        f"{facts.to_prompt_context()}"
    )
    instruction = (
        "You are an ADVERSARIAL critic. For EACH hypothesis in <hypotheses> "
        "independently, find concrete facts that REFUTE it (counter-examples). "
        "Do not compare hypotheses with each other and do not rank them.\n\n"
        "Rules:\n"
        "  1. A refutation MUST point to a specific fact_kind in <facts>. "
        "Quote the fact_kind verbatim.\n"
        "  2. If a fact is marked ✗ (checked and NOT observed), and the "
        "hypothesis needs it to be true, that's a refutation. A fact marked "
        "? (unknown: the check could not be performed) is NOT a refutation "
        "— never cite a ? fact as evidence against a hypothesis.\n"
        "  3. If an observed fact directly contradicts the cause "
        "(e.g. recent_deploy observed but hypothesis blames hardware), "
        "that's a refutation.\n"
        "  4. A caveat, low confidence or weak attribution attached to an "
        "OBSERVED fact (e.g. 'attribution=namespace', 'may not have touched "
        "it') is NOT a refutation — the fact is still observed. Refute only "
        "with ✗ facts or observed facts that contradict the cause.\n"
        "  5. Judge each hypothesis on its own. Do not reuse the same "
        "objection for every hypothesis unless it really refutes each one.\n"
        "  6. If you find NO refutations for a hypothesis after honest "
        "analysis, give it an empty list. Do not invent weaknesses.\n"
        "  7. Each refutation is one short sentence; at most 3 per hypothesis.\n\n"
        "Output VALID JSON ONLY, exactly one entry per hypothesis id:\n"
        '  {"critiques": [{"id": "h1", "refutations": ["..."]}, ...]}\n'
        "No prose, no markdown fences."
    )
    return user_context, instruction


def _parse_batch(raw: str, ids: List[str]) -> Dict[str, List[str]]:
    """id → refutations только для id, по которым ответ разобрался.

    Отсутствующий в результате id = вердикта нет (вызывающий ставит
    BATCH_NO_VERDICT). Чужие id и повторы игнорируются: первый ответ по
    id выигрывает, чтобы модель не могла «переписать» вердикт вторым.
    """
    if not raw or not raw.strip():
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        logger.warning("critic_batch_parse_failed", raw_head=raw[:200])
        return {}
    entries = data.get("critiques") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    wanted = set(ids)
    out: Dict[str, List[str]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        hid = str(e.get("id", ""))
        refs = e.get("refutations")
        if hid not in wanted or hid in out or not isinstance(refs, list):
            continue
        out[hid] = [str(r) for r in refs if r]
    return out


def _precheck(h: Hypothesis, facts: FactStore) -> Tuple[Hypothesis, bool]:
    """Algo-часть критики, общая для обоих режимов.

    Возвращает (гипотеза, нужна_ли_LLM). Algo-refutation — гипотеза уже
    опровергнута, LLM не нужна. Иначе — копия с мягким down-weight-ом
    (если anchor в soft-зоне), которую дальше смотрит LLM.
    """
    algo = _algorithmic_refutations(h, facts)
    if algo:
        # Если уже на дешёвой проверке валится — LLM не дёргаем,
        # это экономит токены и даёт стабильный воспроизводимый
        # refutation для обвалившихся anchor-ов.
        track_refuted("algo")
        return h.model_copy(update={"refutations": algo}), False

    # Мягкий down-weight для «слабых-но-observed» anchor-ов (soft-зона).
    # Гипотеза НЕ опровергается (survivors её пропустит), но confidence
    # снижается ДО прогона LLM, чтобы уверенные конкуренты честно её
    # обошли в best_candidate/synthesis. Grounding и LLM-refutation-логику
    # это не трогает — только ранжирование единственного слабого сигнала.
    penalty = _algorithmic_confidence_penalty(h, facts)
    if penalty < 1.0:
        working = h.model_copy(
            update={"confidence": round(h.confidence * penalty, 4)}
        )
        logger.debug(
            "critic_soft_downweight",
            hypothesis_cause=h.cause,
            from_confidence=h.confidence,
            to_confidence=working.confidence,
        )
        return working, True
    return h, True


class _BatchCritic(BaseAgent):
    """LLM-вызов пакетного режима.

    Отдельный агент, а не второй метод FactCriticAgent: у него своя роль,
    а по роли replay golden-eval (llm_replay) ключует записи. Общая роль
    подсунула бы пакетному вызову записанный ответ per-hypothesis режима
    («n-й ответ той же роли») — разобрался бы как «вердиктов нет», и кейс
    упал бы не из-за модели, а из-за записи.
    """

    def __init__(self) -> None:
        super().__init__(
            name="FactCriticBatch",
            role=(
                "Adversarial fact-checker for a batch of hypotheses. For each "
                "hypothesis find facts that refute it. Never judge "
                "persuasiveness, only evidence consistency."
            ),
            task_type="critic",
            json_response=True,
        )


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
            # Ответ — строго JSON {"refutations":[...]}; обрезка по max_tokens
            # = битый JSON → ask поднимает LLMTruncatedResponse вместо огрызка,
            # который _parse_refutations молча превратил бы в [].
            json_response=True,
        )

    @trace_agent("FactCritic")
    async def critique(
        self, hypothesis: Hypothesis, facts: FactStore
    ) -> Hypothesis:
        working, needs_llm = _precheck(hypothesis, facts)
        if not needs_llm:
            return working

        user_context, instruction = _llm_refutation_prompt(working, facts)
        _t0 = time.monotonic()
        try:
            raw = await self.ask(user_context=user_context, instruction=instruction)
        except LLMTruncatedResponse:
            track_stage_duration("llm_critic", time.monotonic() - _t0)
            # Обрезанный по max_tokens JSON — НЕ «критик не нашёл противоречий».
            # Молчание здесь (ветка ниже) дало бы слабой гипотезе пережить
            # критику с полной confidence. Пробрасываем: stage_critique фейлится
            # терминально (LLMTruncatedResponse не в RETRIABLE_EXC → post-mortem
            # + FAILED в tasks.py) — честнее уверенно-неверного вывода людям.
            raise
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
        """Прогон всех гипотез из set-а в режиме FACT_CRITIC_MODE.

        per_hypothesis — последовательно, по вызову на гипотезу: параллелить
        не нужно, у каждой гипотезы свой кэш в LLM-провайдере не сработает,
        а скачок 3× нагрузки на API вреднее, чем доп. латентность.
        """
        if settings.FACT_CRITIC_MODE == "batch":
            return await self._critique_batch(hyp_set, facts)
        critiqued: List[Hypothesis] = []
        for h in hyp_set.items:
            critiqued.append(await self.critique(h, facts))
        return HypothesisSet(items=critiqued)

    @trace_agent("FactCriticBatch")
    async def _critique_batch(
        self, hyp_set: HypothesisSet, facts: FactStore
    ) -> HypothesisSet:
        """Algo-проверка по каждой, затем один LLM-вызов на все оставшиеся.

        Порядок гипотез в результате — исходный: вызывающие (отчёт, replay)
        не должны зависеть от режима критика.
        """
        out: List[Optional[Hypothesis]] = []
        pending: List[Tuple[int, Hypothesis]] = []
        for i, h in enumerate(hyp_set.items):
            working, needs_llm = _precheck(h, facts)
            out.append(working)
            if needs_llm:
                pending.append((i, working))

        top_n = settings.FACT_CRITIC_BATCH_TOP_N
        if top_n > 0 and len(pending) > top_n:
            # Самые уверенные — модели, остальные не проверены и не выживают.
            # Стабильная сортировка: при равной confidence — исходный порядок.
            ranked = sorted(pending, key=lambda p: -p[1].confidence)
            pending, skipped = ranked[:top_n], ranked[top_n:]
            for i, h in skipped:
                track_refuted("batch_not_reviewed")
                out[i] = h.model_copy(update={"refutations": [BATCH_NOT_REVIEWED]})
            pending.sort(key=lambda p: p[0])

        if pending:
            verdicts = await self._ask_batch(pending, facts)
            for i, h in pending:
                refs = verdicts.get(i)
                if refs is None:
                    track_refuted("batch_no_verdict")
                    logger.warning(
                        "critic_batch_missing_verdict", hypothesis_cause=h.cause,
                    )
                    out[i] = h.model_copy(update={"refutations": [BATCH_NO_VERDICT]})
                    continue
                if refs:
                    track_refuted("llm")
                out[i] = h.model_copy(update={"refutations": refs})
        return HypothesisSet(items=[h for h in out if h is not None])

    async def _ask_batch(
        self, pending: List[Tuple[int, Hypothesis]], facts: FactStore
    ) -> Dict[int, Optional[List[str]]]:
        """index → refutations; None / отсутствие ключа — вердикта нет.

        Пакет больше FACT_CRITIC_BATCH_MAX режется на части. Обрезка ответа
        по max_tokens — не повод ронять стадию: пакет делится пополам и
        переспрашивается, пока не останется одна гипотеза; обрезка на одной —
        тот же терминальный LLMTruncatedResponse, что и в per_hypothesis.
        """
        size = max(1, settings.FACT_CRITIC_BATCH_MAX)
        if len(pending) > size:
            merged: Dict[int, Optional[List[str]]] = {}
            for start in range(0, len(pending), size):
                merged.update(await self._ask_batch(pending[start:start + size], facts))
            return merged

        ids = [f"h{n + 1}" for n in range(len(pending))]
        user_context, instruction = _batch_prompt(
            [(hid, h) for hid, (_, h) in zip(ids, pending)], facts
        )
        _t0 = time.monotonic()
        try:
            raw = await _BatchCritic().ask(
                user_context=user_context, instruction=instruction
            )
        except LLMTruncatedResponse:
            track_stage_duration("llm_critic", time.monotonic() - _t0)
            if len(pending) == 1:
                raise
            half = len(pending) // 2
            logger.warning("critic_batch_truncated_split", size=len(pending))
            first = await self._ask_batch(pending[:half], facts)
            first.update(await self._ask_batch(pending[half:], facts))
            return first
        except Exception as e:
            track_stage_duration("llm_critic", time.monotonic() - _t0)
            if len(pending) > 1:
                # Сбой пакета — чаще всего таймаут на длинном ответе (замер
                # 24.09: claude_cli, пакет из 6 гипотез упирался в 180 с). В
                # per_hypothesis один отказ стоил одной непроверенной гипотезы,
                # здесь — всего пакета; поэтому делим и переспрашиваем.
                half = len(pending) // 2
                logger.warning(
                    "critic_batch_failed_split",
                    error=type(e).__name__, size=len(pending),
                )
                first = await self._ask_batch(pending[:half], facts)
                first.update(await self._ask_batch(pending[half:], facts))
                return first
            # Паритет с per_hypothesis: LLM недоступна — критик молчит, а не
            # опровергает всё подряд. Fail-closed относится к ОТВЕТУ модели
            # (пропущенный id), а не к её отсутствию: отказ провайдера виден
            # в метриках и audit-е (LLM_CALL_FAILED), отказ по id — только здесь.
            logger.warning(
                "critic_batch_llm_failed",
                error=type(e).__name__, size=len(pending),
            )
            return {i: [] for i, _ in pending}
        track_stage_duration("llm_critic", time.monotonic() - _t0)

        by_id = _parse_batch(raw, ids)
        return {i: by_id.get(hid) for hid, (i, _) in zip(ids, pending)}


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
