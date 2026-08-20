"""One-off backfill: схлопывание фантомных db-узлов (C2).

До фикса #185 `secret_hint` в kg_sync строил `db:<driver>:<host>`-узел в
OWN namespace по угаданному из секрета host'у. Один физический кластер БД
(напр. town-db) при этом размножался в per-namespace копии, раздувая граф и
blast-radius (замер 2026-06-25: 16 реальных БД → 288 узлов, до 24 копий одной).

#185 ОСТАНОВИЛ рост (сверка с реестром + пометка unverified_host), но НЕ чистит
накопленное. Этот модуль — разовый backfill: для каждого `db:<driver>:<host>`
схлопывает все копии в ОДИН канонический узел (lexicographically minimal
namespace — тот же критерий, что `kg_sync._known_db_node_namespaces`), перенося
рёбра на канонический и сливая дубли по UNIQUE(src,dst,kind).

Безопасность:
- dry-run по умолчанию (`apply=False`) — только отчёт, ничего не пишет.
- трогает ТОЛЬКО `db:%`-узлы; реальные сервисы и не-db узлы не затрагиваются.
- per-name SAVEPOINT (`begin_nested`) — битый merge одного имени не валит весь
  проход и не оставляет частичную транзакцию (паттерн KG H2).
- идемпотентность: повторный прогон на уже схлопнутом графе = no-op.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import Service, ServiceEdge

log = logging.getLogger(__name__)


def _canonical_and_dups(nodes: List[Service]) -> Tuple[Service, List[Service]]:
    """Канонический узел = в лексикографически минимальном namespace; остальные — дубли.

    Совпадает с критерием `kg_sync._known_db_node_namespaces`, чтобы backfill
    схлопывал в тот же узел, который going-forward sync считает каноническим.
    """
    ordered = sorted(nodes, key=lambda s: (s.namespace or "", s.id))
    return ordered[0], ordered[1:]


def _merge_extras(dst: Dict[str, Any] | None, src: Dict[str, Any] | None) -> Dict[str, Any]:
    """Shallow-merge extras дубль-ребра в существующее (существующее в приоритете)."""
    out: Dict[str, Any] = dict(src) if isinstance(src, dict) else {}
    if isinstance(dst, dict):
        out.update(dst)  # ключи канонического ребра перебивают дубль
    return out


class PhantomDbCleanupRetired(RuntimeError):
    """Этот backfill больше нельзя применять: его правило оказалось неверным."""


def collapse_phantom_db_nodes(db: Session, apply: bool = False) -> Dict[str, Any]:
    """ОТКЛЮЧЕНО. Схлопывало дубли `db:%`-узлов в лексикографически минимальный.

    Правило «канонический узел = в лексикографически минимальном namespace»
    неверно. Физически таких баз столько, сколько окружений: у каждого сквада
    своё, замер 20.08.2026 — `db:postgres:message` в 56 namespace, и каждый
    узел собирает рёбра только своего окружения. Схлопывание сводило 56
    РАЗНЫХ баз в одну, а минимумом среди `preprod-*`, `prod-*`, `squad-*`
    оказывался `preprod-kingdom1` — так граф начинал утверждать, что прод
    ходит в базу препрода.

    Последствия этого прогона разгребает `db_edge_rehome`: 3676 рёбер
    `uses_db` из живых namespace вели в db-узлы `preprod-kingdom1`, которого
    в кластере нет с 15.08.2026.

    Функция оставлена, а не удалена, ровно затем, чтобы её вызов падал с
    объяснением: она доступна из CLI `app/scripts/cleanup_phantom_db_nodes.py`
    с флагом `--apply`, и молчаливое удаление превратило бы заряженное ружьё
    в ImportError без причины. Правильная операция — `db_edge_rehome`.

    Raises:
        PhantomDbCleanupRetired: всегда, при любом значении `apply` — включая
            dry-run: его отчёт называл дублями 56 законных узлов и подталкивал
            запустить перенос.
    """
    raise PhantomDbCleanupRetired(
        "collapse_phantom_db_nodes отключена: правило «канонический = "
        "лексикографически минимальный namespace» сводило разные физические "
        "базы разных окружений в одну и породило 3676 ложных рёбер "
        "(prod → база удалённого preprod-kingdom1). Используй "
        "app.knowledge_graph.db_edge_rehome.rehome_db_edges — он переносит "
        "рёбра на узел в <realm>-shared окружения источника."
    )


def _collapse_phantom_db_nodes_historical(
    db: Session, apply: bool = False,
) -> Dict[str, Any]:
    """Тело отключённого backfill'а. Сохранено для чтения истории графа.

    Не вызывается. Понять, как именно рёбра оказались на узле
    `preprod-kingdom1`, без этого кода нельзя, а понимать это нужно: тот же
    класс ошибки легко повторить в следующем дедупликаторе.
    """
    rows: List[Service] = db.query(Service).filter(Service.name.like("db:%")).all()

    by_name: Dict[str, List[Service]] = {}
    for s in rows:
        by_name.setdefault(cast(str, s.name), []).append(s)

    plan: List[Tuple[Service, List[Service]]] = []
    for name, nodes in by_name.items():
        if len(nodes) <= 1:
            continue
        canonical, dups = _canonical_and_dups(nodes)
        plan.append((canonical, dups))

    stats: Dict[str, Any] = {
        "distinct_db_names": len(by_name),
        "total_db_nodes": len(rows),
        "duplicate_names": len(plan),
        "nodes_to_delete": sum(len(d) for _, d in plan),
        "edges_repointed": 0,
        "edges_merged": 0,
        "nodes_deleted": 0,
        "applied": False,
    }

    if not apply or not plan:
        log.info(
            "phantom_db_cleanup.dry_run distinct=%d total=%d dup_names=%d to_delete=%d",
            stats["distinct_db_names"], stats["total_db_nodes"],
            stats["duplicate_names"], stats["nodes_to_delete"],
        )
        return stats

    for canonical, dups in plan:
        try:
            with db.begin_nested():
                for dup in dups:
                    edges = (
                        db.query(ServiceEdge)
                        .filter(or_(ServiceEdge.src_id == dup.id,
                                    ServiceEdge.dst_id == dup.id))
                        .all()
                    )
                    for e in edges:
                        new_src = canonical.id if e.src_id == dup.id else e.src_id
                        new_dst = canonical.id if e.dst_id == dup.id else e.dst_id
                        # self-loop после переноса (db→db той же физ.БД) — бессмыслен.
                        if new_src == new_dst:
                            db.delete(e)
                            stats["edges_merged"] += 1
                            db.flush()
                            continue
                        existing = (
                            db.query(ServiceEdge)
                            .filter(ServiceEdge.src_id == new_src,
                                    ServiceEdge.dst_id == new_dst,
                                    ServiceEdge.kind == e.kind)
                            .first()
                        )
                        if existing is not None and existing.id != e.id:
                            # merge в существующее ребро: weight не понижаем (как #185),
                            # extras сливаем, дубль удаляем.
                            merged_weight = max(
                                int(existing.weight or 1), int(e.weight or 1)
                            )
                            merged_extras = _merge_extras(
                                cast(Optional[Dict[str, Any]], existing.extras),
                                cast(Optional[Dict[str, Any]], e.extras),
                            )
                            existing.weight = merged_weight  # type: ignore[assignment]
                            existing.extras = merged_extras  # type: ignore[assignment]
                            db.delete(e)
                            stats["edges_merged"] += 1
                        else:
                            e.src_id = new_src
                            e.dst_id = new_dst
                            stats["edges_repointed"] += 1
                        db.flush()  # чтобы следующий existing-lookup видел изменения
                    db.delete(dup)
                    stats["nodes_deleted"] += 1
                    db.flush()
        except Exception as exc:  # noqa: BLE001 — savepoint откатил это имя, идём дальше
            log.warning(
                "phantom_db_cleanup.name_failed canonical=%s error=%s",
                canonical.name, type(exc).__name__,
            )

    db.commit()
    stats["applied"] = True
    log.info(
        "phantom_db_cleanup.applied nodes_deleted=%d edges_repointed=%d edges_merged=%d",
        stats["nodes_deleted"], stats["edges_repointed"], stats["edges_merged"],
    )
    return stats
