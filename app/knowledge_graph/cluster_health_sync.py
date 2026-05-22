"""Sync global cluster health snapshot из VM → kg_cluster_observations.

Beat-task `kg_cluster_health_sync` каждые ~5 мин. Один snapshot per tick,
дублирует поля ClusterHealth.to_dict() (cpu/mem/disk + pods + crashloops
+ deploy_mismatch + alerts). Используется для:
  * post-mortem контекст («что было в кластере на момент инцидента X»)
  * trend-аналитика в digest'ах («last 24h crashloops trend»)

Идемпотентность: UNIQUE(ts). VMClient.get_cluster_health() уже имеет
retry; если он всё-таки бросает — лог + skip, не падаем fully.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.schema import ClusterObservation

log = logging.getLogger(__name__)


async def _sync_cluster_health_async(db: Session) -> Dict[str, Any]:
    if not settings.VICTORIA_METRICS_URL:
        log.info("cluster_health_sync.skipped reason=no_vm_url")
        return {"skipped": "no_vm_url"}

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
    try:
        health = await vm.get_cluster_health()
    except Exception as e:
        log.warning("cluster_health_sync.fetch_failed err=%s", e)
        return {"error": str(e), "inserted": 0}

    if not health.data_available:
        # nodes_total=0 → VM не ответила или snapshot пустой. Не пишем.
        log.info("cluster_health_sync.skipped reason=data_unavailable")
        return {"skipped": "data_unavailable"}

    ts = datetime.utcnow()
    row = ClusterObservation(
        ts=ts,
        cpu_pct=health.cpu_pct,
        mem_pct=health.mem_pct,
        disk_peak_pct=health.disk_peak_pct,
        pods_running=health.pods_running,
        pods_pending=health.pods_pending,
        pods_failed=health.pods_failed,
        crashloops=health.crashloops,
        deploy_mismatch=health.deploy_mismatch,
        alerts_critical=health.alerts_critical,
        alerts_warning=health.alerts_warning,
        alerts_prod=health.alerts_prod,
        raw=health.to_dict(),
    )
    try:
        with db.begin_nested():
            db.add(row)
        db.commit()
        log.info(
            "cluster_health_sync.done status=%s cpu=%.1f mem=%.1f alerts_prod=%d",
            health.health_status, health.cpu_pct, health.mem_pct,
            health.alerts_prod,
        )
        return {"inserted": 1, "ts": ts.isoformat()}
    except IntegrityError:
        db.rollback()
        log.info("cluster_health_sync.skipped_dup ts=%s", ts.isoformat())
        return {"inserted": 0, "skipped_dup": 1}


def sync_cluster_health(db: Session) -> Dict[str, Any]:
    """Sync-обёртка для Celery."""
    return asyncio.run(_sync_cluster_health_async(db))


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(sync_cluster_health(db))
    finally:
        db.close()
