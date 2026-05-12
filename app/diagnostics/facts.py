"""Структуры данных deterministic-уровня."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Fact:
    """Одно наблюдение над инцидентом.

    Принципиально: `observed=False` тоже значимо. Если правило OOMKilledRule
    отработало и нашло «событий OOMKilled нет в окне» — мы фиксируем это
    как явный факт, чтобы LLM не выдвигал гипотезу «возможно OOM» без
    оснований.
    """

    kind: str                      # стабильный slug, см. FactKind ниже
    observed: bool                 # True = факт зафиксирован; False = явное «нет»
    confidence: float              # 0..1, насколько правило уверено в своём наблюдении
    evidence: Dict[str, Any] = field(default_factory=dict)
    subject: Optional[str] = None  # service/pod/namespace, к которому факт относится
    source_rule: Optional[str] = None  # имя правила-источника (для отладки)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Канонические kind-slug-и. Дублирование строк в правилах ловится тестом.
class FactKind:
    OOM_KILLED = "oom_killed"
    CRASHLOOP = "crashloop"
    FAILED_SCHEDULING = "failed_scheduling"
    RECENT_DEPLOY = "recent_deploy"
    RESOURCE_PRESSURE = "resource_pressure"
    UPSTREAM_DEGRADED = "upstream_degraded"

    ALL = frozenset({
        OOM_KILLED, CRASHLOOP, FAILED_SCHEDULING,
        RECENT_DEPLOY, RESOURCE_PRESSURE, UPSTREAM_DEGRADED,
    })


class FactStore:
    """Контейнер фактов одного прогона диагностики.

    Только сборщик и query-API. Никакой логики интерпретации.
    """

    def __init__(self, facts: Optional[List[Fact]] = None) -> None:
        self._facts: List[Fact] = list(facts or [])

    def add(self, fact: Fact) -> None:
        self._facts.append(fact)

    def extend(self, facts: List[Fact]) -> None:
        self._facts.extend(facts)

    @property
    def facts(self) -> List[Fact]:
        return list(self._facts)

    def by_kind(self, kind: str) -> List[Fact]:
        return [f for f in self._facts if f.kind == kind]

    def observed_kinds(self) -> set:
        """kind-ы, у которых хотя бы один наблюдённый факт."""
        return {f.kind for f in self._facts if f.observed}

    def has_observed(self, kind: str) -> bool:
        return any(f.observed for f in self.by_kind(kind))

    def to_dict(self) -> Dict[str, Any]:
        return {"facts": [f.to_dict() for f in self._facts]}

    def to_prompt_context(self) -> str:
        """JSON-подобный блок для подмеса в LLM-промпт.

        Формат стабильный, потому что hypothesis/critic-агенты будут на
        него ссылаться по полям. См. C/D в плане.
        """
        if not self._facts:
            return "<facts>no deterministic facts collected</facts>"
        lines = ["<facts>"]
        for f in self._facts:
            marker = "✓" if f.observed else "✗"
            ev_preview = ", ".join(f"{k}={v}" for k, v in f.evidence.items())
            subj = f" subject={f.subject}" if f.subject else ""
            lines.append(
                f"  {marker} {f.kind} (conf={f.confidence:.2f}){subj}"
                + (f" — {ev_preview}" if ev_preview else "")
            )
        lines.append("</facts>")
        return "\n".join(lines)
