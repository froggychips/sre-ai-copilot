"""D2-auto: drift cleanup в auto-режиме.

`run_drift_cleanup(db, max_drift_pct=20.0, apply=True)` — основная функция:

1. Через `kubectl get ns` собирает live-namespace set.
2. Сравнивает с `kg_services.namespace` distinct.
3. **Safety threshold**: если drift > max_drift_pct (default 20%) — no-op,
   возвращает skipped=True. Защита от kubectl-failure / временной недоступности
   API server, когда вернётся пустой set и все ns "drift".
4. Если apply=True — UPDATE kg_services в drift-ns: `synthetic=true` +
   `metadata.drift_marked_at` + `metadata.drift_reason`.

Возвращает dict-stats для logging/celery.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import Service

log = logging.getLogger(__name__)


class DriftCleanupSkipped(Exception):
    """Raised когда safety threshold спасает от accidental mass-mark."""


def _k8s_live_namespaces() -> Set[str]:
    """kubectl get ns → set имён. Raises на kubectl-failure."""
    out = subprocess.run(
        [
            "kubectl", "get", "ns",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        ],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"kubectl get ns failed (rc={out.returncode}): "
            f"{out.stderr.strip()[:200]}"
        )
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def run_drift_cleanup(
    db: Session,
    max_drift_pct: float = 20.0,
    apply: bool = True,
) -> Dict[str, Any]:
    """Drift cleanup с safety.

    max_drift_pct: если drift > N% от kg-ns — abort (защита от false-wipe
    при kubectl-failure). Default 20% — нормальная гранулярность ns в WO
    (drift обычно 1-3 ns из 47).

    Returns:
        {
          "kg_ns_count": int,
          "k8s_ns_count": int,
          "drift_ns": List[str],
          "drift_pct": float,
          "skipped_threshold": bool,
          "marked_services": int,  # 0 если apply=False или skipped
          "applied": bool,
        }
    """
    k8s_ns = _k8s_live_namespaces()
    kg_ns = {ns for (ns,) in db.query(Service.namespace).distinct().all()}
    drift = sorted(kg_ns - k8s_ns)

    stats: Dict[str, Any] = {
        "kg_ns_count": len(kg_ns),
        "k8s_ns_count": len(k8s_ns),
        "drift_ns": drift,
        "drift_pct": round(100.0 * len(drift) / max(len(kg_ns), 1), 2),
        "skipped_threshold": False,
        "marked_services": 0,
        "applied": False,
    }

    if not drift:
        log.info("drift_cleanup.no_drift kg_ns=%d", len(kg_ns))
        return stats

    if stats["drift_pct"] > max_drift_pct:
        # Защита от false-positive (kubectl вернул пусто из-за временной
        # ошибки — все ns стали бы "drift"). В норме drift 1-5%, threshold
        # 20% это широкая страховка с большим room для растущей инфры.
        stats["skipped_threshold"] = True
        log.warning(
            "drift_cleanup.threshold_exceeded drift_pct=%.2f max=%.2f drift_ns=%s",
            stats["drift_pct"], max_drift_pct, drift,
        )
        return stats

    if not apply:
        return stats

    # PG: metadata_json — SQLAlchemy.JSON, в Postgres хранится как json.
    # Для merge через `||` нужен cast в jsonb. Используем ORM-loop для
    # idempotent merge — проще чем raw SQL с cast-проблемами.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    affected = db.query(Service).filter(Service.namespace.in_(drift)).all()
    marked = 0
    for s in affected:
        if s.synthetic and (s.metadata_json or {}).get("drift_reason"):
            continue  # уже помечен ранее — пропускаем
        s.synthetic = True
        meta = dict(s.metadata_json or {})
        meta["drift_marked_at"] = now_iso
        meta["drift_reason"] = "ns_not_in_k8s"
        s.metadata_json = meta
        marked += 1
    db.commit()
    stats["marked_services"] = marked
    stats["applied"] = True
    log.info(
        "drift_cleanup.applied drift_ns=%d marked_services=%d drift_pct=%.2f",
        len(drift), marked, stats["drift_pct"],
    )
    return stats
