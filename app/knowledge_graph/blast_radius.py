"""Blast radius: «что сломается, если X сломается» — с доказательствами.

`queries.blast_radius_for` (15.08.2026) отвечает на соседний вопрос — «через
какие точки входа ко мне ходят»: k8s Service-узлы (`serves_traffic`) и хосты
ingress (`routes_to`), один шаг, число уверенности на элемент. Кто именно
пострадает, там не сказано.

Здесь — сами пострадавшие. Обход идёт ПРОТИВ зависимостей от цели:

    calls          src вызывает dst  → если dst упал, src деградирует;
    uses_db        src читает БД dst → если БД упала, src без данных;
    uses_nats      src на NATS dst   → если NATS упал, src без шины;
    serves_traffic Service-узел src маршрутизирует на workload dst → точка
                   входа мертва; дальше идём к тем, кто вызывает этот Service.

До `max_hops` шагов (по умолчанию 2). Замер 06.09.2026: у 1748 сервисов один
вызывающий, у 399 — от двух до четырёх, у трёх — больше пяти; БД
`prod-shared/db:postgres:config` держит 111 сервисов. Два шага покрывают
«сервис → его фасад → клиенты фасада», глубже граф уже не про этот инцидент.

Каждое ребро проходит `epistemic.classify_edge`: наблюдали (endpoints с
готовыми подами, runtime), прочитали из манифеста (ingress, Service) или
вывели (env-переменная, имя секрета). Пострадавший наследует САМОЕ СЛАБОЕ
звено своего пути — цепочка из наблюдённого и догадки остаётся догадкой, а
противоречие на любом звене делает весь путь противоречивым. Замер: у
`calls` три источника — ingress (declared), env_url_v2 и env_vars
(inferred), — и ни одного runtime-наблюдения; у `serves_traffic` 98 рёбер,
где топология говорит «обслуживает», а endpoints — «ноль готовых подов».

Known Unknowns — часть ответа. Нет ни одного ребра `calls` к цели —
значит, вызывающие НЕИЗВЕСТНЫ, а не «их нет». Все вызывающие известны лишь
из манифестов и env — список может быть неполным, и это сказано явно.
Рёбра с `extras.inactive` (источник перестал их подтверждать) в обход не
идут, но их число названо.

Обратная совместимость: результат содержит все ключи `blast_radius_for`
(`services`, `urls`, `*_total`, `*_detailed`, `min_confidence_seen`) — на них
смотрят Discord-embed и тесты — плюс `impact`, `summary`, `unknowns`.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple, cast

from sqlalchemy.orm import Session

from app.knowledge_graph.epistemic import (EPISTEMIC_BADGE, EPISTEMIC_WEIGHT,
                                           Epistemic, EpistemicVerdict,
                                           classify_edge,
                                           find_edge_contradictions)
from app.knowledge_graph.queries import blast_radius_for
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service, ServiceEdge)

#: Виды рёбер, по которым сбой распространяется от dst к src.
DEPENDENT_KINDS: Tuple[str, ...] = ("calls", "uses_db", "uses_nats", "serves_traffic")
DEFAULT_MAX_HOPS = 2
DEFAULT_LIMIT = 200

#: Источники `calls`, наблюдающие фактические вызовы. Пока в графе таких
#: рёбер нет (06.09.2026), и это одна из главных Known Unknowns ответа.
_RUNTIME_CALL_SOURCES = frozenset({
    "kg_sync/runtime_seen", "kg_sync/otel_runtime", "kg_sync/vm_runtime",
    "kg_sync/runtime_corr",
})


def _edge_sources(edge: ServiceEdge) -> List[str]:
    extras: Dict[str, Any] = edge.extras if isinstance(edge.extras, dict) else {}
    sources = [s for s in (extras.get("discovery_sources") or []) if isinstance(s, str)]
    discovered_by = cast(Optional[str], edge.discovered_by)
    if discovered_by and discovered_by not in sources:
        sources.append(discovered_by)
    return sources


def _edge_verdict(edge: ServiceEdge) -> EpistemicVerdict:
    extras: Dict[str, Any] = edge.extras if isinstance(edge.extras, dict) else {}
    return classify_edge(
        _edge_sources(edge),
        cast(Any, edge.last_seen_at),
        contradictions=find_edge_contradictions(
            kind=cast(str, edge.kind), src_metadata=extras,
        ),
    )


def _weaker(a: Epistemic, b: Epistemic) -> Epistemic:
    """Слабейшее звено пути. CONTRADICTED побеждает всё: противоречие на
    одном ребре делает весь вывод спорным, сколько бы наблюдений ни было
    рядом."""
    if Epistemic.CONTRADICTED in (a, b):
        return Epistemic.CONTRADICTED
    return a if EPISTEMIC_WEIGHT[a] <= EPISTEMIC_WEIGHT[b] else b


def _resolve_target(db: Session, namespace: str, name: str) -> Optional[Service]:
    """Service-узел, а если такого нет — любой узел с этим именем: цель может
    быть БД (`db:postgres:config`) или NATS, у них node_kind свой."""
    # node_kind в каждом фильтре обязателен (сторож test_node_kind_lookup_
    # ambiguity): одноимённые service- и workload-узлы иначе дают
    # MultipleResultsFound. Порядок: service → db/nats/ingress → workload.
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name,
                Service.node_kind == NODE_KIND_SERVICE)
        .one_or_none()
    )
    if svc is not None:
        return svc
    other = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name,
                Service.node_kind != NODE_KIND_WORKLOAD)
        .order_by(Service.id)
        .first()
    )
    if other is not None:
        return other
    return (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name,
                Service.node_kind == NODE_KIND_WORKLOAD)
        .order_by(Service.id)
        .first()
    )


def blast_radius_v2(
    db: Session,
    namespace: str,
    service_name: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    limit: int = DEFAULT_LIMIT,
    top_n: int = 3,
    include_inactive: bool = False,
) -> Dict[str, Any]:
    base = blast_radius_for(db, namespace, service_name, top_n=top_n)
    target = _resolve_target(db, namespace, service_name)
    if target is None:
        return {**base, "target": None, "impact": [], "summary": _summary([], max_hops),
                "unknowns": [{"scope": "target", "reason": "сервис не найден в графе"}]}

    target_ids: Set[int] = {cast(int, target.id)}
    for (wid,) in (
        db.query(Service.id)
        .filter(Service.namespace == namespace, Service.name == service_name,
                Service.node_kind == NODE_KIND_WORKLOAD)
        .all()
    ):
        target_ids.add(int(wid))

    # BFS против рёбер зависимости. Узел учитывается один раз — первым
    # найденным путём (кратчайшим); пути одинаковой длины не сравниваем:
    # обход по слоям гарантирует, что первый найденный — не длиннее других.
    impacted: Dict[int, Dict[str, Any]] = {}
    visited: Set[int] = set(target_ids)
    frontier: Deque[Tuple[int, List[str], Epistemic, int]] = deque(
        (tid, [service_name], Epistemic.OBSERVED, 0) for tid in target_ids
    )
    inactive_skipped = 0
    calls_edges_seen = 0
    calls_observed = 0
    truncated = False
    overflow_nodes = 0

    while frontier:
        node_id, path, path_epistemic, hops = frontier.popleft()
        if hops >= max_hops:
            continue
        edges: List[ServiceEdge] = (
            db.query(ServiceEdge)
            .filter(ServiceEdge.dst_id == node_id, ServiceEdge.kind.in_(DEPENDENT_KINDS))
            .all()
        )
        for edge in edges:
            src = edge.src
            if src is None:
                continue
            extras: Dict[str, Any] = edge.extras if isinstance(edge.extras, dict) else {}
            if extras.get("inactive") and not include_inactive:
                inactive_skipped += 1
                continue
            kind = cast(str, edge.kind)
            # «Вызывающие цели» — только прямые рёбра calls к ней (hops == 0):
            # runtime-наблюдение на втором шаге ничего не говорит о первом.
            if kind == "calls" and hops == 0:
                calls_edges_seen += 1
                if any(s in _RUNTIME_CALL_SOURCES for s in _edge_sources(edge)):
                    calls_observed += 1
            src_id = cast(int, src.id)
            if src_id in visited:
                continue
            visited.add(src_id)
            verdict = _edge_verdict(edge)
            epistemic = _weaker(path_epistemic, verdict.status)
            new_path = [*path, cast(str, src.name)]
            if len(impacted) >= limit:
                truncated = True
                overflow_nodes += 1
                continue
            impacted[src_id] = {
                "service": src.name,
                "namespace": src.namespace,
                "node_kind": src.node_kind,
                "via": kind,
                "hops": hops + 1,
                "path": new_path,
                "epistemic": epistemic.value,
                "badge": EPISTEMIC_BADGE[epistemic],
                "edge_epistemic": verdict.status.value,
                "reasons": list(verdict.reasons),
                "conflicts": list(verdict.conflicts),
                "last_seen_at": edge.last_seen_at,
            }
            frontier.append((src_id, new_path, epistemic, hops + 1))

    # Остались ли узлы за горизонтом: у последнего слоя есть свои зависимые?
    if not truncated:
        last_layer = [nid for nid, e in impacted.items() if e["hops"] == max_hops]
        if last_layer:
            beyond = (
                db.query(ServiceEdge.src_id)
                .filter(ServiceEdge.dst_id.in_(last_layer), ServiceEdge.kind.in_(DEPENDENT_KINDS))
                .distinct()
                .all()
            )
            overflow_nodes = len({int(s) for (s,) in beyond if int(s) not in visited})

    impact = sorted(
        impacted.values(),
        key=lambda e: (-EPISTEMIC_WEIGHT[Epistemic(e["epistemic"])], e["hops"], e["service"]),
    )
    unknowns = _unknowns(
        impact, calls_edges_seen=calls_edges_seen, calls_observed=calls_observed,
        inactive_skipped=inactive_skipped, max_hops=max_hops,
        overflow_nodes=overflow_nodes, truncated=truncated, limit=limit,
    )
    return {
        **base,
        "target": {"namespace": target.namespace, "name": target.name,
                   "node_kind": target.node_kind},
        "impact": impact,
        "summary": _summary(impact, max_hops),
        "unknowns": unknowns,
    }


def _summary(impact: List[Dict[str, Any]], max_hops: int) -> Dict[str, Any]:
    by_epistemic: Dict[str, int] = defaultdict(int)
    by_via: Dict[str, int] = defaultdict(int)
    by_hops: Dict[int, int] = defaultdict(int)
    for e in impact:
        by_epistemic[e["epistemic"]] += 1
        by_via[e["via"]] += 1
        by_hops[e["hops"]] += 1
    worst: Optional[str] = None
    if impact:
        worst = min(
            (Epistemic(e["epistemic"]) for e in impact),
            key=lambda ep: (ep is not Epistemic.CONTRADICTED, EPISTEMIC_WEIGHT[ep]),
        ).value
    return {
        "impacted_total": len(impact),
        "by_epistemic": dict(by_epistemic),
        "by_via": dict(by_via),
        "by_hops": {str(k): v for k, v in sorted(by_hops.items())},
        "max_hops": max_hops,
        "worst_epistemic": worst,
    }


def _unknowns(
    impact: List[Dict[str, Any]], *, calls_edges_seen: int, calls_observed: int,
    inactive_skipped: int, max_hops: int, overflow_nodes: int, truncated: bool, limit: int,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if calls_edges_seen == 0:
        out.append({
            "scope": "callers",
            "reason": "в графе нет ни одного ребра calls к цели — вызывающие "
                      "неизвестны, а не отсутствуют (источники calls: ingress, "
                      "env-переменные; runtime-наблюдений вызовов нет)",
        })
    elif calls_observed == 0:
        out.append({
            "scope": "callers",
            "reason": f"все {calls_edges_seen} рёбер calls известны из манифестов и "
                      "env (declared/inferred), runtime-наблюдений вызовов в графе нет — "
                      "список вызывающих может быть неполным",
        })
    if inactive_skipped:
        out.append({
            "scope": "inactive_edges",
            "reason": f"{inactive_skipped} рёбер помечены inactive (источник перестал их "
                      "подтверждать) и в обход не вошли",
        })
    if truncated:
        out.append({
            "scope": "limit",
            "reason": f"обход остановлен на {limit} пострадавших; за пределом ещё ≥{overflow_nodes}",
        })
    elif overflow_nodes:
        out.append({
            "scope": "hops",
            "reason": f"обход остановлен на {max_hops} шагах; у последнего слоя ещё "
                      f"{overflow_nodes} зависимых узлов",
        })
    contradicted = [e for e in impact if e["epistemic"] == Epistemic.CONTRADICTED.value]
    if contradicted:
        out.append({
            "scope": "contradicted",
            "reason": f"{len(contradicted)} путей проходят через противоречивое ребро "
                      "(источники утверждают разное) — требуют проверки, не решения",
        })
    return out
