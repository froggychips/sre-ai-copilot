"""Переатрибутировать KubeJobFailed с vm-kube-state-metrics на владельца Job.

До 07.09.2026 store-путь резолвил цель kube-алертов только по
deployment/statefulset/daemonset; алерты про Job (`job_name`) падали на
лейбл `service` = vm-kube-state-metrics. За 30 дней — 66 алертов, все на
KSM, и инциденты 1.0.7 (kg_incidents) их унаследовали.

Что делает:
  * берёт kg_alerts с alertname из _ALERTNAMES, чей сервис — KSM;
  * имя Job — из raw.description («Job <ns>/<job> failed to complete…»),
    ns — из description же (у KSM-узла ns тот, где стоит экспортер? нет —
    populate писал ns инцидента, но перепроверяем по description);
  * цель — job_attribution.resolve_job_target (владелец из kg_k8s_jobs,
    иначе имя CronJob);
  * upsert service-узла цели, перевод alert.service_id, снятие алерта с
    KSM-инцидента (пустой инцидент удаляется) и attach к инциденту цели.

CLI:
    python -m app.scripts.reattribute_job_alerts            # dry-run
    python -m app.scripts.reattribute_job_alerts --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.knowledge_graph.incidents import attach_alert
from app.knowledge_graph.job_attribution import resolve_job_target
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, AlertEvent,
                                        KGIncident, Service)

_ALERTNAMES = ("KubeJobFailed", "KubeJobNotCompleted")
_KSM = "vm-kube-state-metrics"
_DESC_RE = re.compile(r"Job (?P<ns>[a-z0-9-]+)/(?P<job>[a-z0-9.-]+) ", re.I)


def _job_from_raw(raw: Any) -> Optional[Dict[str, str]]:
    desc = (raw or {}).get("description") if isinstance(raw, dict) else None
    m = _DESC_RE.search(desc or "")
    return {"ns": m.group("ns"), "job": m.group("job")} if m else None


def _detach_from_incident(db: Session, alert: AlertEvent) -> Optional[str]:
    """Снять алерт с текущего инцидента; вернуть, что стало с инцидентом."""
    if not alert.incident_id:
        return None
    inc = db.query(KGIncident).filter(KGIncident.incident_key == alert.incident_id).one_or_none()
    if inc is None:
        return None
    current: Any = inc.fingerprints or []
    fps = [fp for fp in current if fp != alert.fingerprint]
    if not fps:
        db.delete(inc)
        return "deleted"
    row: Any = inc
    row.fingerprints = fps
    row.alert_count = len(fps)
    others = (
        db.query(AlertEvent.alertname)
        .filter(AlertEvent.fingerprint.in_(fps))
        .distinct()
        .all()
    )
    row.alertnames = sorted({a for (a,) in others})
    return "shrunk"


def reattribute(db: Session, *, apply: bool = False) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"candidates": 0, "resolved": 0, "unparsed": 0, "moved": 0,
                             "incidents_deleted": 0, "incidents_shrunk": 0, "applied": apply,
                             "by_how": {}, "sample": []}
    rows: List[AlertEvent] = (
        db.query(AlertEvent)
        .join(Service, Service.id == AlertEvent.service_id)
        .filter(AlertEvent.alertname.in_(_ALERTNAMES), Service.name == _KSM)
        .order_by(AlertEvent.fired_at)
        .all()
    )
    stats["candidates"] = len(rows)
    for alert in rows:
        job = _job_from_raw(alert.raw)
        if not job:
            stats["unparsed"] += 1
            continue
        target, how = resolve_job_target(db, job["ns"], job["job"])
        if not target:
            stats["unparsed"] += 1
            continue
        stats["resolved"] += 1
        stats["by_how"][how] = stats["by_how"].get(how, 0) + 1
        if len(stats["sample"]) < 8:
            stats["sample"].append(f"{job['ns']}/{job['job']} → {target} ({how})")
        if not apply:
            continue
        svc = upsert_service(db, namespace=job["ns"], name=target, node_kind=NODE_KIND_SERVICE)
        db.flush()
        fate = _detach_from_incident(db, alert)
        if fate == "deleted":
            stats["incidents_deleted"] += 1
        elif fate == "shrunk":
            stats["incidents_shrunk"] += 1
        alert_row: Any = alert
        alert_row.service_id = svc.id
        alert_row.incident_id = None
        db.flush()
        attach_alert(
            db, namespace=job["ns"], service_name=target, service_id=cast(int, svc.id),
            fired_at=cast(Any, alert.fired_at), alertname=cast(str, alert.alertname),
            severity=cast(Optional[str], alert.severity), fingerprint=cast(Optional[str], alert.fingerprint),
        )
        stats["moved"] += 1
    if apply:
        db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = reattribute(db, apply=args.apply)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("\ndry-run: --apply чтобы перенести.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
