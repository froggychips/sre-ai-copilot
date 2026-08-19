"""Эпистемический статус факта в графе: откуда мы это знаем и стоит ли верить.

К августу 2026 в графе накопилось семь параллельных осей «насколько мы
уверены»: `OWNER_SOURCE_TRUST`, `_SOURCE_PRECEDENCE`, `stale_class`,
`synthetic`, `node_kind`, состояние namespace и `QUALITY_THRESHOLDS`. Каждая
осмысленна, но общего словаря нет — и потребитель графа не может задать
простой вопрос: «этот факт наблюдали или вывели, и не спорит ли с ним
что-нибудь ещё».

Здесь такой словарь. Он не заменяет существующие шкалы, а собирает их ответ
в один статус.

Главное, чего раньше не было вовсе, — **CONTRADICTED**. Граф умел сказать
«знаю» и «не знаю», но не умел «источники утверждают разное». Между тем
такие случаи на живых данных не редкость (замер 19.08.2026):

  * 98 рёбер `serves_traffic`, у которых источник-топология говорит
    «Service обслуживает этот workload», а endpoints — «за ним ноль готовых
    подов». Оба источника наблюдательные, и оба правы по-своему: манифест
    описывает намерение, endpoints — факт;
  * 2192 узла обновляются в namespace, помеченных `missing`: синк видит их
    живыми, жизненный цикл — исчезнувшими.

До этой модели такие расхождения выглядели обычными фактами. Худший вид
неправды: уверенный ответ, собранный из двух несогласных источников.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


class Epistemic(str, Enum):
    """Откуда известен факт. Порядок — от сильного к слабому.

    `CONTRADICTED` стоит особняком: это не степень уверенности, а сигнал, что
    вопрос требует разбирательства. Потребителю честнее показать конфликт,
    чем выбрать одну из сторон за него.
    """

    #: Наблюдали фактическое состояние: endpoints с готовыми подами,
    #: реальный вызов в трейсе. Сильнее манифеста — манифест описывает
    #: намерение, а не то, что происходит.
    OBSERVED = "observed"
    #: Прочитано из k8s-ресурса: Service.spec.selector, Ingress.rules.
    #: Объявление существует физически, но исполняется ли — другой вопрос.
    DECLARED = "declared"
    #: Подтверждено несколькими независимыми источниками. Отдельный статус,
    #: потому что согласие двух слабых источников сильнее одного слабого.
    CORROBORATED = "corroborated"
    #: Выведено косвенно: env-переменная, имя секрета, соглашение об
    #: именовании. Догадка, пусть и обоснованная.
    INFERRED = "inferred"
    #: Источник давно не подтверждал факт. Не ложь — истёкшая свежесть.
    STALE = "stale"
    #: Источники утверждают разное. Требует разбирательства, а не усреднения.
    CONTRADICTED = "contradicted"
    #: Провенанса нет вовсе — запись из эпохи до учёта источников.
    UNKNOWN = "unknown"


#: Насколько статус пригоден для автоматических решений. `CONTRADICTED`
#: намеренно ниже `INFERRED`: догадка честно называет себя догадкой, а
#: противоречие выглядит фактом, пока его не назвали.
EPISTEMIC_WEIGHT: Dict[Epistemic, float] = {
    Epistemic.OBSERVED: 1.0,
    Epistemic.CORROBORATED: 0.9,
    Epistemic.DECLARED: 0.8,
    Epistemic.INFERRED: 0.5,
    Epistemic.STALE: 0.3,
    Epistemic.CONTRADICTED: 0.2,
    Epistemic.UNKNOWN: 0.0,
}

#: Значок для пользовательских ответов. Живёт здесь, а не у каждого
#: потребителя: три копии соответствия неизбежно разъедутся.
EPISTEMIC_BADGE: Dict[Epistemic, str] = {
    Epistemic.OBSERVED: "✓",
    Epistemic.CORROBORATED: "✓",
    Epistemic.DECLARED: "◇",
    Epistemic.INFERRED: "≈",
    Epistemic.STALE: "⌛",
    Epistemic.CONTRADICTED: "⚠",
    Epistemic.UNKNOWN: "?",
}

#: Источники, наблюдающие фактическое состояние.
_OBSERVING_SOURCES = frozenset({
    "k8s_endpoints/ready",
    "kg_sync/runtime_seen",
    "kg_sync/otel_runtime",
    "kg_sync/vm_runtime",
    "kg_sync/runtime_corr",
})

#: Источники, читающие манифест.
_DECLARING_SOURCES = frozenset({
    "kg_sync/ingress",
    "kg_sync/service",
    "kg_sync/network_policy",
    "k8s_topology_resources/service",
    "k8s_topology_resources/ingress",
    "k8s_jobs_sync/job",
    "k8s_jobs_sync/cronjob",
    "k8s_storage/pod_volumes",
    "k8s_storage/pvc_spec",
})

#: Сколько дней без подтверждения делают факт устаревшим. Совпадает с
#: порогом `edge_decay` для мягкой пометки — чтобы две системы не спорили.
STALE_AFTER_DAYS = 7


@dataclass(frozen=True)
class EpistemicVerdict:
    """Статус факта и объяснение, почему он такой.

    `reasons` заполняются всегда, а не только при конфликте: «почему граф так
    считает» — вопрос, который задают чаще, чем сам статус.
    """

    status: Epistemic
    reasons: List[str]
    conflicts: List[str]

    @property
    def weight(self) -> float:
        return EPISTEMIC_WEIGHT[self.status]

    @property
    def badge(self) -> str:
        return EPISTEMIC_BADGE[self.status]

    @property
    def is_actionable(self) -> bool:
        """Можно ли опираться на факт без участия человека.

        Противоречие — не «слабый факт», а вопрос без ответа: автоматике на
        него опираться нельзя, сколько бы источников ни было.
        """
        return self.status not in (Epistemic.CONTRADICTED, Epistemic.UNKNOWN)


def classify_edge(
    sources: Optional[Sequence[str]],
    last_seen_at: Optional[datetime],
    *,
    contradictions: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> EpistemicVerdict:
    """Эпистемический статус ребра графа.

    `contradictions` — уже найденные конфликты (см. `find_edge_contradictions`).
    Они перекрывают всё остальное: сколько бы источников ни подтверждало
    факт, если другой источник его опровергает, ответ — «разбирайтесь».
    """
    conflicts = list(contradictions or [])
    if conflicts:
        return EpistemicVerdict(Epistemic.CONTRADICTED,
                                reasons=["источники утверждают разное"],
                                conflicts=conflicts)

    known = [s for s in (sources or []) if s]
    if not known:
        return EpistemicVerdict(Epistemic.UNKNOWN,
                                reasons=["провенанс не записан"], conflicts=[])

    reasons: List[str] = []
    age_days = None
    if last_seen_at is not None:
        age_days = ((now or datetime.utcnow()) - last_seen_at) / timedelta(days=1)

    observing = [s for s in known if s in _OBSERVING_SOURCES]
    declaring = [s for s in known if s in _DECLARING_SOURCES]

    # Устаревание проверяется ДО силы источника: наблюдение месячной
    # давности — уже не наблюдение, а воспоминание.
    if age_days is not None and age_days > STALE_AFTER_DAYS:
        reasons.append(f"последнее подтверждение {age_days:.0f} дн назад")
        return EpistemicVerdict(Epistemic.STALE, reasons, conflicts=[])

    if observing:
        reasons.append(f"наблюдалось: {', '.join(sorted(observing))}")
        return EpistemicVerdict(Epistemic.OBSERVED, reasons, conflicts=[])

    if len(set(known)) > 1:
        reasons.append(f"подтверждено независимо: {len(set(known))} источника")
        return EpistemicVerdict(Epistemic.CORROBORATED, reasons, conflicts=[])

    if declaring:
        reasons.append(f"объявлено в k8s: {', '.join(sorted(declaring))}")
        return EpistemicVerdict(Epistemic.DECLARED, reasons, conflicts=[])

    reasons.append(f"выведено: {', '.join(sorted(known))}")
    return EpistemicVerdict(Epistemic.INFERRED, reasons, conflicts=[])


def find_edge_contradictions(
    *,
    kind: str,
    src_metadata: Optional[Dict[str, Any]] = None,
    namespace_state: Optional[str] = None,
) -> List[str]:
    """Известные виды расхождений между источниками.

    Список намеренно короткий и конкретный: каждый пункт — случай, увиденный
    на живых данных, а не гипотеза. Общего механизма «сравнить все источники»
    здесь нет и, вероятно, не нужно — противоречия возникают в предсказуемых
    местах.
    """
    found: List[str] = []
    meta = src_metadata if isinstance(src_metadata, dict) else {}

    # Топология: «Service обслуживает workload». Endpoints: «готовых подов
    # ноль». Оба наблюдательны, и оба правы по-своему — манифест описывает
    # намерение, endpoints фиксирует факт. Замер 19.08.2026: 98 таких рёбер.
    if kind == "serves_traffic" and meta.get("endpoints_ready") == 0:
        found.append(
            "топология считает Service обслуживающим, но endpoints "
            "сообщает о нуле готовых подов"
        )

    # Синк обновляет узел, а жизненный цикл namespace считает его
    # исчезнувшим. Замер 19.08.2026: 2192 таких узла.
    if namespace_state == "missing":
        found.append(
            "namespace помечен исчезнувшим, но узел продолжает обновляться"
        )

    return found
