"""Читающее API инцидентов графа: список, карточка, timeline.

Монтируется в app.main с router-level `get_current_user` — те же правила,
что у /replay. Идентификатор в URL — целочисленный `id`: `incident_key`
содержит `/` и `@` и в путь не годится, но возвращается в теле.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.knowledge_graph.incident_timeline import build_timeline
from app.knowledge_graph.incidents import incident_to_dict
from app.knowledge_graph.schema import AlertEvent, KGIncident

router = APIRouter()


def _get_or_404(db: Session, incident_id: int) -> KGIncident:
    inc = db.query(KGIncident).filter(KGIncident.id == incident_id).one_or_none()
    if inc is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return inc


@router.get("")
def list_incidents(
    status: Optional[str] = Query(None, pattern="^(open|resolved)$"),
    namespace: Optional[str] = None,
    service: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    q = db.query(KGIncident)
    if status:
        q = q.filter(KGIncident.status == status)
    if namespace:
        q = q.filter(KGIncident.namespace == namespace)
    if service:
        q = q.filter(KGIncident.service_name == service)
    rows: List[KGIncident] = q.order_by(KGIncident.opened_at.desc()).limit(limit).all()
    return {"incidents": [incident_to_dict(i) for i in rows], "count": len(rows)}


@router.get("/{incident_id}")
def get_incident(incident_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    inc = _get_or_404(db, incident_id)
    fps = list(inc.fingerprints or [])
    alerts: List[AlertEvent] = []
    if fps:
        alerts = (
            db.query(AlertEvent)
            .filter(AlertEvent.fingerprint.in_(fps))
            .order_by(AlertEvent.fired_at)
            .all()
        )
    return {
        **incident_to_dict(inc),
        "alerts": [
            {"alertname": a.alertname, "severity": a.severity, "fingerprint": a.fingerprint,
             "fired_at": a.fired_at, "resolved_at": a.resolved_at}
            for a in alerts
        ],
    }


@router.get("/{incident_id}/timeline")
def get_incident_timeline(incident_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    inc = _get_or_404(db, incident_id)
    return build_timeline(db, inc)
