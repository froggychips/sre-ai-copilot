"""Перевес рёбер `uses_db` с db-узлов удалённых окружений на правильные.

Зачем это понадобилось. `phantom_db_cleanup` (#189) схлопывал копии
`db:<driver>:<host>`-узлов в ОДИН узел на всю базу, выбирая канонический как
лексикографически минимальный namespace. Правило оказалось неверным: физически
таких баз столько, сколько окружений — у каждого сквада своё, — а минимумом
среди `preprod-*`, `prod-*`, `squad-*` оказался `preprod-kingdom1`. В него и
уехали рёбра со всего кластера.

Фикс 15.08.2026 (`contract.shared_namespace_of`) исправил going-forward: новые
рёбра идут в `<realm>-shared`. Но накопленное осталось, и 20.08.2026 в графе
было 3676 рёбер `uses_db` из ЖИВЫХ namespace в db-узлы `preprod-kingdom1` —
namespace'а, которого в кластере нет с 15.08. Граф утверждал, что прод ходит в
базу удалённого препрода: не потеря точности, а неверный факт о проде, ровно в
том запросе, ради которого граф существует (blast radius).

Что делает этот модуль: для каждого такого ребра находит узел с тем же именем
в `shared_namespace_of(src.namespace)` и переводит ребро на него. Замер перед
запуском: приёмник существует для всех 3676 рёбер, то есть угадывать не
приходится ни в одном случае.

Чего НЕ делает:

  * не создаёт узлы. Нет приёмника — ребро остаётся как было. Выдуманный узел
    хуже устаревшего: устаревший виден проверкой `graph_integrity`, выдуманный
    неотличим от настоящего.
  * не удаляет исходные db-узлы. У них могут быть рёбра внутри собственного
    мёртвого окружения (~102 на снесённый сквад) — это законная история, её
    убирает retention в `namespace_lifecycle`, а не этот модуль.
  * не трогает рёбра мёртвое→мёртвое. Ссылка снесённого сквада на свою же базу
    — не ложь о работающей системе.

Безопасность:
  * dry-run по умолчанию (`apply=False`) — считает и показывает план;
  * SAVEPOINT на батч: сбой одного не откатывает уже перенесённое;
  * коммит на батч, а не на весь проход — иначе транзакция держит блокировки
    на тысячах строк, пока рядом пишут синки (авария 15.08.2026 с
    DeadlockDetected в endpoints-синке);
  * идемпотентность: повторный прогон на исправленном графе — no-op.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.orm import Session, aliased

from app.knowledge_graph.contract import shared_namespace_of
from app.knowledge_graph.schema import Namespace, Service, ServiceEdge

log = logging.getLogger(__name__)

__all__ = ["rehome_db_edges", "DB_EDGE_KINDS"]

#: Виды рёбер, которые ведут НА базу. Замер 20.08.2026: из живых namespace в
#: db-узлы мёртвых шли только `uses_db` (3676 штук) и только в этом
#: направлении — db-узел как dst. Список явный, чтобы случайный новый вид
#: ребра не поехал переноситься без разбора.
DB_EDGE_KINDS = ("uses_db",)

#: Рёбер на коммит. Того же порядка, что `_COMMIT_BATCH` в endpoints-синке.
_BATCH = 200


def _target_node(
    db: Session,
    dead_dst: Service,
    src_namespace: str,
    cache: Dict[Tuple[str, str], Optional[Service]],
) -> Optional[Service]:
    """Узел той же базы в `<realm>-shared` окружения источника, или None.

    Кэш нужен потому, что 3676 рёбер приходятся на ~12 имён баз и ~100
    окружений: без него это тысячи одинаковых SELECT'ов.
    """
    target_ns = shared_namespace_of(src_namespace)
    if not target_ns:
        # Namespace без распознаваемого realm (`sre-ai`, `monitoring`).
        # Придумывать ему shared-пару не на чем.
        return None
    key = (cast(str, dead_dst.name), target_ns)
    if key in cache:
        return cache[key]
    found = (
        db.query(Service)
        .filter(Service.name == dead_dst.name,
                Service.namespace == target_ns,
                Service.node_kind == dead_dst.node_kind)
        .first()
    )
    cache[key] = found
    return found


def _stale_db_edges(db: Session) -> List[int]:
    """id рёбер `uses_db` из active-namespace в db-узел missing-namespace."""
    src_s = aliased(Service)
    dst_s = aliased(Service)
    ns_src = aliased(Namespace)
    ns_dst = aliased(Namespace)
    rows = (
        db.query(ServiceEdge.id)
        .join(dst_s, ServiceEdge.dst_id == dst_s.id)
        .join(src_s, ServiceEdge.src_id == src_s.id)
        .join(ns_dst, ns_dst.namespace == dst_s.namespace)
        .join(ns_src, ns_src.namespace == src_s.namespace)
        .filter(dst_s.name.like("db:%"),
                ServiceEdge.kind.in_(DB_EDGE_KINDS),
                ns_dst.state == "missing",
                ns_src.state == "active")
        # Детерминированный порядок: два писателя, идущие по строкам в разном
        # порядке, блокируют друг друга крест-накрест.
        .order_by(ServiceEdge.id)
        .all()
    )
    return [r[0] for r in rows]


def rehome_db_edges(db: Session, apply: bool = False) -> Dict[str, Any]:
    """Перевести рёбра на db-узлы живых окружений.

    Возвращает статистику: сколько нашли, сколько перевесили, сколько слилось
    с уже существующим ребром, сколько оставили без приёмника.
    """
    edge_ids = _stale_db_edges(db)
    stats: Dict[str, Any] = {
        "stale_edges": len(edge_ids),
        "repointed": 0,
        "merged": 0,
        "no_target": 0,
        "batches_failed": 0,
        "applied": False,
    }

    if not edge_ids:
        log.info("db_edge_rehome.nothing_to_do")
        return stats

    if not apply:
        # Даже в dry-run считаем, для скольких приёмник есть: без этого числа
        # план не читается — «3676 рёбер» не говорит, выполним ли перенос.
        cache: Dict[Tuple[str, str], Optional[Service]] = {}
        for eid in edge_ids:
            edge = db.get(ServiceEdge, eid)
            if edge is None:
                continue
            dst = db.get(Service, edge.dst_id)
            src = db.get(Service, edge.src_id)
            if dst is None or src is None:
                continue
            if _target_node(db, dst, cast(str, src.namespace), cache) is None:
                stats["no_target"] += 1
            else:
                stats["repointed"] += 1
        log.info(
            "db_edge_rehome.dry_run stale=%d resolvable=%d no_target=%d",
            stats["stale_edges"], stats["repointed"], stats["no_target"],
        )
        return stats

    cache = {}
    for offset in range(0, len(edge_ids), _BATCH):
        chunk = edge_ids[offset:offset + _BATCH]
        try:
            with db.begin_nested():
                _rehome_batch(db, chunk, cache, stats)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — savepoint откатил батч
            stats["batches_failed"] += 1
            log.warning(
                "db_edge_rehome.batch_failed offset=%d size=%d error=%s",
                offset, len(chunk), type(exc).__name__,
            )
            db.rollback()

    stats["applied"] = True
    log.info(
        "db_edge_rehome.applied repointed=%d merged=%d no_target=%d failed_batches=%d",
        stats["repointed"], stats["merged"], stats["no_target"],
        stats["batches_failed"],
    )
    return stats


def _rehome_batch(
    db: Session,
    edge_ids: List[int],
    cache: Dict[Tuple[str, str], Optional[Service]],
    stats: Dict[str, Any],
) -> None:
    """Перенести один батч. Коммит — на вызывающей стороне."""
    for eid in edge_ids:
        edge = db.get(ServiceEdge, eid)
        if edge is None:
            continue          # снесено параллельным drift_cleanup
        src = db.get(Service, edge.src_id)
        dst = db.get(Service, edge.dst_id)
        if src is None or dst is None:
            continue
        target = _target_node(db, dst, cast(str, src.namespace), cache)
        if target is None:
            stats["no_target"] += 1
            continue
        if target.id == edge.src_id:
            # Сервис и его база схлопнулись бы в петлю. Такое ребро
            # бессмысленно: удаляем, а не переносим.
            db.delete(edge)
            stats["merged"] += 1
            db.flush()
            continue
        existing = (
            db.query(ServiceEdge)
            .filter(ServiceEdge.src_id == edge.src_id,
                    ServiceEdge.dst_id == target.id,
                    ServiceEdge.kind == edge.kind)
            .first()
        )
        if existing is not None and existing.id != edge.id:
            # Правильное ребро уже есть — going-forward синк его создал.
            # Вес не понижаем (тот же приём в phantom_db_cleanup), устаревший
            # дубль удаляем.
            merged_weight = max(int(existing.weight or 1), int(edge.weight or 1))
            existing.weight = merged_weight  # type: ignore[assignment]
            db.delete(edge)
            stats["merged"] += 1
        else:
            edge.dst_id = target.id  # type: ignore[assignment]
            stats["repointed"] += 1
        db.flush()   # чтобы следующий lookup existing видел изменение
