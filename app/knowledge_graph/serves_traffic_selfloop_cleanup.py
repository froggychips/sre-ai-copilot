"""One-off backfill: удаление `serves_traffic` self-loop рёбер (src_id == dst_id).

До guard в `k8s_topology_resources_sync` билдер `serves_traffic` (Service →
backing Deployment) плодил ребро сам-на-себя для КАЖДОГО сервиса, чьё имя
Service совпадает с именем Deployment в том же namespace: граф ключевал узлы по
`(name, namespace)` без разделителя типа, поэтому Service `foo` и Deployment
`foo` были ОДНИМ `kg_services`-узлом, а ребро между ними — self-loop.

С contract 2.4 корень устранён: у узла есть `node_kind`, Service и workload —
разные строки, и билдер строит нормальное cross-node ребро. Guard там остался
только страховкой. Этот модуль по-прежнему нужен для уже накопленных данных —
новых self-loop он не увидит.

Эти рёбра засоряют blast-radius (`queries.get_blast_radius`): сервис попадает в
собственный список «кто пострадает» (serves_traffic IN-edges). Guard остановил
рост going-forward; этот модуль чистит уже накопленное.

Безопасность:
- dry-run по умолчанию (`apply=False`) — только считает, ничего не удаляет.
- трогает ТОЛЬКО рёбра `kind='serves_traffic' AND src_id == dst_id`; cross-node
  serves_traffic и self-loop'ы других kind не затрагиваются.
- идемпотентность: повторный прогон на уже вычищенном графе = no-op.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import ServiceEdge

log = logging.getLogger(__name__)

SELF_LOOP_KIND = "serves_traffic"


def delete_serves_traffic_self_loops(db: Session, apply: bool = False) -> Dict[str, Any]:
    """Удалить serves_traffic self-loop рёбра (src_id == dst_id).

    Returns dict-stats: self_loops_found, deleted, applied.
    При apply=False (dry-run) deleted=0, applied=False.
    """
    q = db.query(ServiceEdge).filter(
        ServiceEdge.src_id == ServiceEdge.dst_id,
        ServiceEdge.kind == SELF_LOOP_KIND,
    )
    found = q.count()
    stats: Dict[str, Any] = {
        "self_loops_found": found,
        "deleted": 0,
        "applied": False,
    }

    if not apply or found == 0:
        log.info("serves_traffic_selfloop_cleanup.dry_run found=%d", found)
        return stats

    deleted = q.delete(synchronize_session=False)
    db.commit()
    stats["deleted"] = int(deleted)
    stats["applied"] = True
    log.info("serves_traffic_selfloop_cleanup.applied deleted=%d", deleted)
    return stats
