"""PodEventsRule — факты из k8s Events.

Читает ctx["k8s_events"] (список dict-ов от K8sFacts.collect_snapshot
или recent_pod_events_for из KG) и маппит event reason → FactKind.
В отличие от regex-правил, source — структурированные данные k8s API,
поэтому confidence выше.

Маппинг event reason → FactKind:
    OOMKilling, OOMKilled        → oom_killed       (0.95)
    FailedScheduling             → failed_scheduling (0.95)
    Evicted                      → resource_pressure (0.90)
    BackOff, CrashLoopBackOff    → crashloop         (0.85)
    MemoryPressure, DiskPressure → resource_pressure (0.85)

СКОУПИНГ (инцидент «уверенная неверная причина в Discord»): когда
K8sFacts зовут без pod-label, в ctx["k8s_events"] лежат Warning-события
ВСЕГО namespace-а. Раньше любое из них давало Fact(observed=True,
conf=0.95) — OOM НЕсвязанного сервиса становился неопровержимым anchor-ом
(observed-факт критик опровергнуть не может) и уезжал в Discord как
причина чужого инцидента. Теперь событие сверяется с target-workload
(тот же same_workload, что уже используется для k8s_pod_state в oom.py /
process_crash.py):

    объект события того же workload-а  → observed=True, полная confidence;
    объект события ЧУЖОГО workload-а   → observed=False + evidence-пометка
                                         (сигнал не теряется, но anchor-ом
                                          не становится);
    привязку проверить нечем (у алерта
    нет ни pod, ни service, либо у
    события нет involvedObject)        → observed=True, но confidence
                                         срезана вдвое → попадает в
                                         soft-зону fact_critic
                                         ([0.25, 0.5)) и мягко
                                         штрафуется вместо анкеровки.

Правило намеренно не дублирует ✗-сигналы «событий такого типа не было»:
отсутствие события — не доказательство отсутствия факта. Единственный ✗,
который оно эмитит, — про ЧУЖУЮ привязку («у target такого события нет,
оно у другого workload-а»), и это как раз наблюдение, а не отсутствие
данных.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule, same_workload
from app.knowledge_graph.epistemic import Epistemic

_SOURCE = "k8s_events"
_PROVENANCE = "k8s_event"

# (reason_lower_prefix, fact_kind, confidence)
_REASON_MAP: List[Tuple[str, str, float]] = [
    ("oomkill",          FactKind.OOM_KILLED,        0.95),
    ("evict",            FactKind.RESOURCE_PRESSURE,  0.90),
    ("memorypressure",   FactKind.RESOURCE_PRESSURE,  0.85),
    ("diskpressure",     FactKind.RESOURCE_PRESSURE,  0.85),
    ("failedscheduling", FactKind.FAILED_SCHEDULING,  0.95),
    ("crashloopbackoff", FactKind.CRASHLOOP,          0.85),
    ("backoff",          FactKind.CRASHLOOP,          0.75),
]

# Множитель для событий с непроверяемой привязкой (у алерта нет target
# либо у события нет involvedObject). 0.5 уводит максимум 0.95 → 0.475,
# т.е. в soft-зону fact_critic: гипотеза выживает, но не якорится жёстко.
_UNVERIFIED_CONFIDENCE_FACTOR = 0.5
# Уверенность в ✗ «у target этого события нет — оно у чужого workload-а».
# Умеренная: мы видели выборку событий namespace-а, а не полную историю.
_FOREIGN_OBJECT_CONFIDENCE = 0.5

# Классы привязки события к target-workload.
_SCOPED = "scoped"        # объект события = target workload
_FOREIGN = "foreign"      # объект события — другой workload namespace-а
_UNVERIFIED = "unverified"  # проверить нечем


def _match_reason(reason: str) -> Tuple[str, float] | None:
    r = reason.lower()
    for prefix, kind, conf in _REASON_MAP:
        if r.startswith(prefix) or prefix in r:
            return kind, conf
    return None


def _event_object(ev: Dict[str, Any]) -> str:
    """Имя объекта события.

    Поддерживаем все три формы, которые реально приходят:
      * `object`     — K8sFacts._parse_events (involved_object.name);
      * `pod_name`   — recent_pod_events_for из KG;
      * `involvedObject` — сырой k8s JSON (dict со `name` или строка).
    """
    for key in ("object", "pod_name"):
        val = ev.get(key)
        if isinstance(val, str) and val:
            return val
    involved = ev.get("involvedObject")
    if isinstance(involved, dict):
        name = involved.get("name")
        if isinstance(name, str):
            return name
    if isinstance(involved, str):
        return involved
    return ""


def _attribution(obj: str, target: Optional[str]) -> str:
    if not target or not obj:
        return _UNVERIFIED
    return _SCOPED if same_workload(obj, target) else _FOREIGN


class PodEventsRule(Rule):
    name = "PodEventsRule"
    sources = (_SOURCE,)

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        events = ctx.get(_SOURCE) or []
        target = ctx.get("pod") or ctx.get("service")
        if not events:
            # Known Unknowns: пустой список при живом источнике — по-прежнему
            # тишина (отсутствие события не доказывает отсутствие факта, см.
            # докстринг). Но если источник событий недоступен, молчать
            # нельзя: критик прочтёт «anchor oom_killed NOT observed» как
            # опровержение, а мы событий даже не видели. На каждый kind, о
            # котором правило умеет говорить, — явный ?.
            problem = self.source_problem(ctx, _SOURCE)
            if not problem:
                return []
            seen: List[str] = []
            for _prefix, kind, _conf in _REASON_MAP:
                if kind not in seen:
                    seen.append(kind)
            subject = target or ctx.get("namespace")
            return [
                Fact.unknown(kind, problem, subject=subject,
                             source_rule=self.name, provenance="kg_pod_events")
                for kind in seen
            ]

        # Агрегируем по (класс привязки, kind): берём максимальную confidence
        # среди событий. Один OOMKilling-ивент target-workload-а достаточен.
        best: Dict[str, Dict[str, Tuple[float, Dict[str, Any]]]] = {
            _SCOPED: {}, _FOREIGN: {}, _UNVERIFIED: {},
        }
        for ev in events:
            match = _match_reason(ev.get("reason", ""))
            if match is None:
                continue
            kind, conf = match
            obj = _event_object(ev)
            bucket = best[_attribution(obj, target)]
            if kind not in bucket or conf > bucket[kind][0]:
                bucket[kind] = (
                    conf,
                    {
                        "source": "k8s_event",
                        "reason": ev["reason"],
                        "message": ev.get("message", "")[:120],
                        "count": ev.get("count", 1),
                        "object": obj,
                    },
                )

        fallback_subject = target or ctx.get("namespace")
        facts: List[Fact] = []

        for kind, (conf, evidence) in best[_SCOPED].items():
            facts.append(
                Fact(
                    kind=kind,
                    observed=True,
                    confidence=conf,
                    subject=evidence.get("object") or fallback_subject,
                    evidence=evidence,
                    source_rule=self.name,
                    epistemic=Epistemic.OBSERVED.value,
                    provenance=_PROVENANCE,
                )
            )

        for kind, (conf, evidence) in best[_UNVERIFIED].items():
            if kind in best[_SCOPED]:
                continue  # подтверждённая привязка сильнее — не дублируем
            facts.append(
                Fact(
                    kind=kind,
                    observed=True,
                    confidence=round(conf * _UNVERIFIED_CONFIDENCE_FACTOR, 4),
                    subject=fallback_subject,
                    evidence={
                        **evidence,
                        "attribution": "unverified",
                        "note": (
                            "namespace-wide event; alert has no pod/service "
                            "label to attribute it to the target workload"
                        ),
                    },
                    source_rule=self.name,
                    # Событие наблюдали, но принадлежность target-у — догадка.
                    epistemic=Epistemic.INFERRED.value,
                    provenance=_PROVENANCE,
                )
            )

        for kind, (_conf, evidence) in best[_FOREIGN].items():
            if kind in best[_SCOPED] or kind in best[_UNVERIFIED]:
                continue  # про target уже есть более релевантный факт
            facts.append(
                Fact(
                    kind=kind,
                    observed=False,
                    confidence=_FOREIGN_OBJECT_CONFIDENCE,
                    subject=fallback_subject,
                    evidence={
                        **evidence,
                        "attribution": "other_workload",
                        "note": (
                            "event belongs to a different workload in the same "
                            "namespace — not evidence about the target"
                        ),
                    },
                    source_rule=self.name,
                    epistemic=Epistemic.OBSERVED.value,
                    provenance=_PROVENANCE,
                )
            )

        return facts
