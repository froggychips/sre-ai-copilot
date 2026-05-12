"""Структурная hypothesis-модель для fan-out + fact-anchoring + critic."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class Hypothesis(BaseModel):
    """Одна гипотеза причины инцидента.

    Ключевое требование архитектуры: `anchored_facts` НЕ ПУСТО. Любая
    гипотеза без anchor-факта отбрасывается на этапе валидации (это и
    защищает от LLM-«свободного рассуждения»).
    """

    cause: str = Field(
        ..., description="Короткое (1 предложение) описание причины."
    )
    detail: str = Field(
        "", description="Развёрнутое объяснение, как причина приводит к alert-у."
    )
    anchored_facts: List[str] = Field(
        ...,
        description=(
            "Список fact_kind (canonical slug-ов из FactKind.ALL), на которые "
            "опирается гипотеза. Должен быть непустым."
        ),
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Самооценка агента, 0..1."
    )
    perspective: str = Field(
        ..., description="Имя perspective-агента: app|infra|deps."
    )
    refutations: List[str] = Field(
        default_factory=list,
        description=(
            "Заполняется критиком на этапе D: какие факты ОПРОВЕРГАЮТ эту "
            "гипотезу. Пустой список = критик не нашёл противоречий."
        ),
    )

    @field_validator("anchored_facts")
    @classmethod
    def _at_least_one_anchor(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("Hypothesis must reference at least one anchor fact.")
        return v


class HypothesisSet(BaseModel):
    """Коллекция гипотез одного fan-out прогона.

    После фильтрации (по существующим observed-фактам) и работы critic-а
    содержит финальный кандидат-сет для synthesis-стадии.
    """

    items: List[Hypothesis] = Field(default_factory=list)

    def filter_grounded(self, observed_fact_kinds: set[str]) -> "HypothesisSet":
        """Оставить только гипотезы, чьи anchored_facts реально observed.

        Это и есть «дисциплина anchor-а»: даже если LLM выдумает causal
        path к kind, которого в FactStore нет — отбрасываем без обсуждения.
        """
        kept = []
        for h in self.items:
            grounded = [f for f in h.anchored_facts if f in observed_fact_kinds]
            if grounded:
                kept.append(h.model_copy(update={"anchored_facts": grounded}))
        return HypothesisSet(items=kept)

    def by_perspective(self) -> dict[str, List[Hypothesis]]:
        out: dict[str, List[Hypothesis]] = {}
        for h in self.items:
            out.setdefault(h.perspective, []).append(h)
        return out

    def consensus_kinds(self) -> List[str]:
        """fact_kind-ы, упомянутые ≥ в 2 разных perspective-ах.

        Сильный сигнал: если app/infra/deps независимо ссылаются на один
        и тот же fact — это, скорее всего, реальная причина.
        """
        from collections import defaultdict
        perspective_per_kind: dict[str, set[str]] = defaultdict(set)
        for h in self.items:
            for kind in h.anchored_facts:
                perspective_per_kind[kind].add(h.perspective)
        return sorted(k for k, ps in perspective_per_kind.items() if len(ps) >= 2)

    def disagreement_signal(self) -> Optional[str]:
        """Если каждая perspective дала разный top-kind — это «не уверены».

        Возвращает строку-сигнал для эскалации на человека. None = ок,
        перспективы хотя бы частично сошлись.
        """
        by_p = self.by_perspective()
        if len(by_p) < 2:
            return None
        top_per_perspective = {}
        for p, hyps in by_p.items():
            best = max(hyps, key=lambda h: h.confidence, default=None)
            if best:
                top_per_perspective[p] = tuple(sorted(best.anchored_facts))
        unique_tops = set(top_per_perspective.values())
        if len(unique_tops) == len(top_per_perspective) and len(unique_tops) > 1:
            return (
                "perspectives_disagree: each perspective produced a different "
                "top hypothesis — manual triage recommended"
            )
        return None
