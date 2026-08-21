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
  * перенос обратим. В `extras` ребра записывается прежний адрес —
    `rehomed_from` (namespace исходного db-узла) и `rehomed_at`. Без этого
    операция необратима: `dst_id` перезаписывается, и восстановить, куда
    ребро смотрело, потом нечем. Ключи с префиксом `rehomed_` и есть
    журнал отката, `scripts/rehome_db_edges.py --undo` читает именно их.
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
from sqlalchemy.orm.attributes import flag_modified

from app.core.timeutil import utcnow
from app.knowledge_graph.contract import shared_namespace_of
from app.knowledge_graph.schema import Service, ServiceEdge

log = logging.getLogger(__name__)

__all__ = ["rehome_db_edges", "undo_rehome", "DB_EDGE_KINDS"]

#: Виды рёбер, которые ведут НА базу. Замер 20.08.2026: из живых namespace в
#: db-узлы мёртвых шли только `uses_db` (3676 штук) и только в этом
#: направлении — db-узел как dst. Список явный, чтобы случайный новый вид
#: ребра не поехал переноситься без разбора.
DB_EDGE_KINDS = ("uses_db",)

#: Рёбер на коммит. Того же порядка, что `_COMMIT_BATCH` в endpoints-синке.
_BATCH = 200


def _stamp() -> str:
    """Метка времени переноса. Один формат с остальными metadata графа."""
    return utcnow().isoformat()


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
    """id рёбер `uses_db`, ведущих в базу ЧУЖОГО окружения.

    Два случая, и оба — один и тот же неверный факт:

      * db-узел лежит в удалённом namespace. Замер 21.08.2026: 3740 рёбер
        вели в `preprod-kingdom1`, которого нет в кластере с 15.08.
      * db-узел лежит в ЖИВОМ, но чужом окружении. Первый прогон переноса
        этот случай не покрывал, и после него в графе осталось 1900 таких
        рёбер, включая двенадцать вида «прод-сервис ходит в базу
        препрода» — например все семь `bot-service` из prod-kingdom1..7
        указывали на `preprod-kingdom2/db:postgres:map-coordinator`, при
        том что `prod-shared/db:postgres:map-coordinator` существует.
        Живой namespace-получатель делал ложь незаметной для проверки,
        которая смотрела только на `missing`.

    Источник у обоих один: `phantom_db_cleanup` сводил разные физические
    базы в узел с лексикографически минимальным namespace. Отбор поэтому
    идёт не по состоянию namespace, а по несовпадению окружений: так
    попадают и те рёбра, чей получатель ещё жив.

    Фильтрация по `shared_namespace_of` идёт в Python, а не в SQL: правило
    выделения realm — регулярка в `contract.py`, и дублировать её в
    SQL-выражении значит завести второе место, где оно живёт.
    """
    src_s = aliased(Service)
    dst_s = aliased(Service)
    rows = (
        db.query(ServiceEdge.id, src_s.namespace, dst_s.namespace)
        .join(dst_s, ServiceEdge.dst_id == dst_s.id)
        .join(src_s, ServiceEdge.src_id == src_s.id)
        .filter(dst_s.name.like("db:%"),
                ServiceEdge.kind.in_(DB_EDGE_KINDS))
        # Детерминированный порядок: два писателя, идущие по строкам в разном
        # порядке, блокируют друг друга крест-накрест.
        .order_by(ServiceEdge.id)
        .all()
    )
    out: List[int] = []
    for edge_id, src_ns, dst_ns in rows:
        src_realm = shared_namespace_of(src_ns)
        dst_realm = shared_namespace_of(dst_ns)
        if not src_realm or not dst_realm:
            # Namespace без распознаваемого realm (`sre-ai`, `monitoring`):
            # судить о «своём» и «чужом» окружении для них не на чем.
            continue
        if src_realm != dst_realm:
            out.append(edge_id)
    return out


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
            extras = dict(cast(Optional[Dict[str, Any]], edge.extras) or {})
            # Прежний адрес — чтобы перенос можно было отменить. Пишем
            # namespace, а не id: id узла может исчезнуть вместе с узлом, а
            # (name, namespace) остаётся читаемым и без него.
            extras["rehomed_from"] = dst.namespace
            extras["rehomed_at"] = _stamp()
            edge.extras = extras  # type: ignore[assignment]
            flag_modified(edge, "extras")
            edge.dst_id = target.id  # type: ignore[assignment]
            stats["repointed"] += 1
        db.flush()   # чтобы следующий lookup existing видел изменение


def undo_rehome(db: Session, apply: bool = False) -> Dict[str, Any]:
    """Вернуть перенесённые рёбра на прежние узлы по журналу в `extras`.

    Читает `rehomed_from` — namespace, где db-узел лежал до переноса, — и
    ищет узел с тем же именем там. Узел мог быть удалён за это время
    (retention снёс мёртвое окружение): такое ребро оставляем на месте и
    считаем в `target_gone`. Возвращать его некуда, и выдумывать узел ради
    отката — то же самое, чего избегает сам перенос.

    Слитые рёбра (`merged`) откату не подлежат: устаревший дубль удалён, а
    в существующее правильное ребро перенос не писал ничего, кроме веса.
    Их и не должно быть много — на проде 20.08.2026 слияний ожидалось ноль,
    потому что going-forward синк создал только 36 правильных рёбер.
    """
    rows: List[ServiceEdge] = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.kind.in_(DB_EDGE_KINDS))
        .order_by(ServiceEdge.id)
        .all()
    )
    marked = [
        e for e in rows
        if isinstance(e.extras, dict) and e.extras.get("rehomed_from")
    ]
    stats: Dict[str, Any] = {
        "marked_edges": len(marked),
        "restored": 0,
        "target_gone": 0,
        "conflict": 0,
        "applied": False,
    }
    if not marked or not apply:
        log.info("db_edge_rehome.undo_dry_run marked=%d", len(marked))
        return stats

    for edge in marked:
        extras = dict(cast(Dict[str, Any], edge.extras))
        origin_ns = extras.get("rehomed_from")
        current = db.get(Service, edge.dst_id)
        if current is None:
            continue
        origin = (
            db.query(Service)
            .filter(Service.name == current.name,
                    Service.namespace == origin_ns,
                    Service.node_kind == current.node_kind)
            .first()
        )
        if origin is None:
            stats["target_gone"] += 1
            continue
        clash = (
            db.query(ServiceEdge)
            .filter(ServiceEdge.src_id == edge.src_id,
                    ServiceEdge.dst_id == origin.id,
                    ServiceEdge.kind == edge.kind)
            .first()
        )
        if clash is not None and clash.id != edge.id:
            # На прежнем адресе уже есть ребро — вернуть значило бы нарушить
            # UNIQUE(src,dst,kind). Оставляем как есть.
            stats["conflict"] += 1
            continue
        extras.pop("rehomed_from", None)
        extras.pop("rehomed_at", None)
        edge.extras = extras  # type: ignore[assignment]
        flag_modified(edge, "extras")
        edge.dst_id = origin.id  # type: ignore[assignment]
        stats["restored"] += 1
        db.flush()

    db.commit()
    stats["applied"] = True
    log.info(
        "db_edge_rehome.undone restored=%d target_gone=%d conflict=%d",
        stats["restored"], stats["target_gone"], stats["conflict"],
    )
    return stats
