"""Заполнение knowledge-graph узлов и рёбер.

Сейчас это stub-уровень: только идемпотентные upsert-методы. Реальный
backfill (читать k8s API, TeamCity history, alertmanager dump) — отдельный
этап после интеграции в pipeline (см. план Э5/Э6).

Зачем уже сейчас держать API:
  * pipeline сможет дописывать AlertEvent при каждом инциденте — это
    наполнит часть графа автоматически.
  * Юнит-тесты UpstreamDegradedRule и nearby_alerts() пишут через
    эти же методы, не дублируя ORM-код.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import (AlertEvent, Deployment, Service,
                                        ServiceEdge)

logger = structlog.get_logger()


def upsert_service(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Service:
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name)
        .one_or_none()
    )
    if svc is None:
        svc = Service(
            namespace=namespace,
            name=name,
            team_owner=team_owner,
            metadata_json=metadata,
        )
        db.add(svc)
        db.flush()
        logger.info("kg.service_created", namespace=namespace, name=name)
    else:
        changed = False
        if team_owner and svc.team_owner != team_owner:
            svc.team_owner = team_owner
            changed = True
        if metadata is not None:
            svc.metadata_json = metadata
            changed = True
        if changed:
            db.flush()
    return svc


def upsert_edge(
    db: Session,
    src: Service,
    dst: Service,
    kind: str,
    weight: int = 1,
    discovered_by: Optional[str] = None,
) -> ServiceEdge:
    edge = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.src_id == src.id,
            ServiceEdge.dst_id == dst.id,
            ServiceEdge.kind == kind,
        )
        .one_or_none()
    )
    if edge is None:
        edge = ServiceEdge(
            src_id=src.id,
            dst_id=dst.id,
            kind=kind,
            weight=weight,
            discovered_by=discovered_by,
        )
        db.add(edge)
        db.flush()
    else:
        if edge.weight != weight:
            edge.weight = weight
            db.flush()
    return edge


def record_deployment(
    db: Session,
    service: Service,
    started_at: datetime,
    sha: Optional[str] = None,
    repo: Optional[str] = None,
    buildtype_id: Optional[str] = None,
    build_number: Optional[str] = None,
    finished_at: Optional[datetime] = None,
    status: Optional[str] = None,
    triggered_by: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Deployment:
    dep = Deployment(
        service_id=service.id,
        started_at=started_at,
        finished_at=finished_at,
        sha=sha,
        repo=repo,
        buildtype_id=buildtype_id,
        build_number=build_number,
        status=status,
        triggered_by=triggered_by,
        extras=extras,
    )
    db.add(dep)
    db.flush()
    return dep


def record_alert_event(
    db: Session,
    service: Optional[Service],
    alertname: str,
    severity: Optional[str],
    fingerprint: Optional[str],
    fired_at: datetime,
    incident_id: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> AlertEvent:
    """Идемпотентно по fingerprint — если уже есть, обновляем resolved_at и raw."""
    existing: Optional[AlertEvent] = None
    if fingerprint:
        existing = (
            db.query(AlertEvent)
            .filter(AlertEvent.fingerprint == fingerprint)
            .one_or_none()
        )
    if existing is not None:
        existing.severity = severity or existing.severity
        if raw is not None:
            existing.raw = raw
        return existing

    ev = AlertEvent(
        service_id=service.id if service else None,
        alertname=alertname,
        severity=severity,
        fingerprint=fingerprint,
        fired_at=fired_at,
        incident_id=incident_id,
        raw=raw,
    )
    db.add(ev)
    db.flush()
    return ev
