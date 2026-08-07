"""Структуры данных deterministic-уровня."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


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
    # Процесс упал от сигнала (SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL).
    # Отличается от oom_killed (SIGKILL=137) тем, что причина — баг в коде,
    # а не нехватка памяти.
    PROCESS_CRASH = "process_crash"

    ALL = frozenset({
        OOM_KILLED, CRASHLOOP, FAILED_SCHEDULING,
        RECENT_DEPLOY, RESOURCE_PRESSURE, UPSTREAM_DEGRADED,
        PROCESS_CRASH,
    })


# Пары взаимоисключающих фактов.
# exit 137 (SIGKILL/OOM) и exit 139+ (SIGSEGV и др.) физически не могут быть
# одновременно причиной ОДНОГО краша — если оба observed=True ДЛЯ ОДНОГО
# subject-а, данные противоречивы. Разные поды (OOM у одного, segfault у
# другого) — не конфликт, а два независимых наблюдения (см. _same_subject).
MUTUALLY_EXCLUSIVE_PAIRS: List[FrozenSet[str]] = [
    frozenset({FactKind.OOM_KILLED, FactKind.PROCESS_CRASH}),
]


def _same_subject(a: Optional[str], b: Optional[str]) -> bool:
    """Конфликт осмыслен только про один subject.

    None = subject неизвестен — консервативно считаем совпадением (лучше
    ложное предупреждение о противоречии, чем скрытое противоречие).
    """
    if a is None or b is None:
        return True
    return a == b


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

    def conflicts(self) -> List[Tuple[Fact, Fact]]:
        """Пары фактов: оба observed=True, взаимоисключающие И про один subject.

        Subject-aware: oom_killed(pod-a) × process_crash(pod-b) для разных
        подов — НЕ конфликт (раньше такая пара давала ложное «data is
        contradictory» и резала обе confidence до 0.60).
        """
        result: List[Tuple[Fact, Fact]] = []
        for pair in MUTUALLY_EXCLUSIVE_PAIRS:
            kinds = list(pair)
            a_facts = [f for f in self._facts if f.kind == kinds[0] and f.observed]
            b_facts = [f for f in self._facts if f.kind == kinds[1] and f.observed]
            for a in a_facts:
                for b in b_facts:
                    if _same_subject(a.subject, b.subject):
                        result.append((a, b))
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {"facts": [f.to_dict() for f in self._facts]}

    def to_prompt_context(self) -> str:
        """JSON-подобный блок для подмеса в LLM-промпт.

        Формат стабильный, потому что hypothesis/critic-агенты будут на
        него ссылаться по полям.
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

        conflict_pairs = self.conflicts()
        if conflict_pairs:
            lines.append("<conflicts>")
            for a, b in conflict_pairs:
                lines.append(
                    f"  WARNING: {a.kind} and {b.kind} are both observed=True "
                    f"but mutually exclusive — data is contradictory, treat both "
                    f"with reduced confidence."
                )
            lines.append("</conflicts>")

        lines.append("</facts>")
        return "\n".join(lines)
