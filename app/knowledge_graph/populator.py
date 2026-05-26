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

from app.knowledge_graph.schema import (AlertEvent, Deployment, PodEvent,
                                        Service, ServiceEdge)

logger = structlog.get_logger()


def _is_postgresql(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def upsert_service(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    synthetic: Optional[bool] = None,
) -> Service:
    """Idempotent upsert.

    На PostgreSQL использует INSERT ON CONFLICT DO UPDATE — атомарно, без
    race condition при параллельных worker'ах.
    На других диалектах (SQLite в тестах) — старый SELECT+INSERT.
    """
    if _is_postgresql(db):
        return _upsert_service_pg(db, namespace, name, team_owner, metadata, synthetic)
    return _upsert_service_fallback(db, namespace, name, team_owner, metadata, synthetic)


def _upsert_service_pg(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str],
    metadata: Optional[Dict[str, Any]],
    synthetic: Optional[bool],
) -> Service:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.utcnow()
    values: Dict[str, Any] = {
        "namespace": namespace,
        "name": name,
        "team_owner": team_owner,
        "metadata_json": metadata,
        "synthetic": bool(synthetic) if synthetic is not None else False,
        "created_at": now,
        "updated_at": now,
    }
    set_clause: Dict[str, Any] = {"updated_at": now}
    if team_owner:
        set_clause["team_owner"] = team_owner
    if metadata is not None:
        set_clause["metadata_json"] = metadata
    if synthetic is not None:
        set_clause["synthetic"] = bool(synthetic)

    stmt = (
        pg_insert(Service.__table__)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_kg_service_ns_name",
            set_=set_clause,
        )
        .returning(Service.__table__.c.id)
    )
    db.execute(stmt)
    db.flush()
    logger.info("kg.service_upserted", namespace=namespace, name=name)
    return db.query(Service).filter_by(namespace=namespace, name=name).one()


def _upsert_service_fallback(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str],
    metadata: Optional[Dict[str, Any]],
    synthetic: Optional[bool],
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
            synthetic=bool(synthetic) if synthetic is not None else False,
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
        if synthetic is not None and svc.synthetic != synthetic:
            svc.synthetic = synthetic
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
    extras: Optional[Dict[str, Any]] = None,
) -> ServiceEdge:
    """Idempotent upsert по (src_id, dst_id, kind).

    На PostgreSQL — INSERT ON CONFLICT, исключает race condition.
    `extras` (JSON): discovery_sources и confidence — merge, не overwrite.
    """
    if _is_postgresql(db):
        return _upsert_edge_pg(db, src, dst, kind, weight, discovered_by, extras)
    return _upsert_edge_fallback(db, src, dst, kind, weight, discovered_by, extras)


def _upsert_edge_pg(
    db: Session,
    src: Service,
    dst: Service,
    kind: str,
    weight: int,
    discovered_by: Optional[str],
    extras: Optional[Dict[str, Any]],
) -> ServiceEdge:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.utcnow()
    initial_extras = dict(extras or {})
    if discovered_by:
        initial_extras.setdefault("discovery_sources", [discovered_by])

    stmt = (
        pg_insert(ServiceEdge.__table__)
        .values(
            src_id=src.id, dst_id=dst.id, kind=kind,
            weight=weight, discovered_by=discovered_by,
            extras=initial_extras or None, last_seen_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_kg_edge_src_dst_kind",
            set_={"last_seen_at": now, "weight": weight,
                  **({"discovered_by": discovered_by} if discovered_by else {})},
        )
    )
    db.execute(stmt)
    db.flush()

    edge = db.query(ServiceEdge).filter_by(src_id=src.id, dst_id=dst.id, kind=kind).one()
    # C3: merge extras + discovery_sources в Python (JSONB merge в SQL сложнее).
    merged = dict(edge.extras or {})
    changed = False
    if extras:
        merged.update(extras)
        changed = True
    if discovered_by:
        sources = list(merged.get("discovery_sources") or [])
        if discovered_by not in sources:
            sources.append(discovered_by)
            merged["discovery_sources"] = sources
            changed = True
    if changed and merged != (edge.extras or {}):
        edge.extras = merged
        db.flush()
    return edge


def _upsert_edge_fallback(
    db: Session,
    src: Service,
    dst: Service,
    kind: str,
    weight: int,
    discovered_by: Optional[str],
    extras: Optional[Dict[str, Any]],
) -> ServiceEdge:
    edge = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.src_id == src.id, ServiceEdge.dst_id == dst.id,
                ServiceEdge.kind == kind)
        .one_or_none()
    )
    now = datetime.utcnow()
    initial_extras = dict(extras or {})
    if discovered_by:
        initial_extras.setdefault("discovery_sources", [discovered_by])

    if edge is None:
        edge = ServiceEdge(
            src_id=src.id, dst_id=dst.id, kind=kind, weight=weight,
            discovered_by=discovered_by, extras=initial_extras or None,
            last_seen_at=now,
        )
        db.add(edge)
        db.flush()
    else:
        edge.last_seen_at = now
        if edge.weight != weight:
            edge.weight = weight
        merged = dict(edge.extras or {})
        if extras:
            merged.update(extras)
        if discovered_by:
            existing_sources = list(merged.get("discovery_sources") or [])
            if discovered_by not in existing_sources:
                existing_sources.append(discovered_by)
            merged["discovery_sources"] = existing_sources
        if merged != (edge.extras or {}):
            edge.extras = merged
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
    # Dedup: один build (buildtype_id + build_number) не должен дублироваться
    # если появляется в нескольких инцидентах.
    if buildtype_id and build_number:
        existing = (
            db.query(Deployment)
            .filter(
                Deployment.service_id == service.id,
                Deployment.buildtype_id == buildtype_id,
                Deployment.build_number == build_number,
            )
            .one_or_none()
        )
        if existing is not None:
            if sha and not existing.sha:
                existing.sha = sha
                db.flush()
            return existing

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
        existing.last_notified_at = datetime.utcnow()
        if raw is not None:
            existing.raw = raw
        return existing

    ev = AlertEvent(
        service_id=service.id if service else None,
        alertname=alertname,
        severity=severity,
        fingerprint=fingerprint,
        fired_at=fired_at,
        last_notified_at=datetime.utcnow(),
        incident_id=incident_id,
        raw=raw,
    )
    db.add(ev)
    db.flush()
    return ev


def record_pod_event(
    db: Session,
    service: Optional[Service],
    namespace: str,
    pod_name: str,
    reason: str,
    event_uid: str,
    first_seen: datetime,
    last_seen: Optional[datetime] = None,
    count: Optional[int] = None,
    message: Optional[str] = None,
    type_: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> PodEvent:
    """A4: идемпотентно по `event_uid` (k8s Event UID).

    Повторный sync того же события → обновляем `last_seen` и `count`
    (k8s агрегирует одинаковые события и инкрементит count).
    """
    existing = (
        db.query(PodEvent).filter(PodEvent.event_uid == event_uid).one_or_none()
    )
    if existing is not None:
        if last_seen is not None:
            existing.last_seen = last_seen
        if count is not None:
            existing.count = count
        return existing

    ev = PodEvent(
        service_id=service.id if service else None,
        namespace=namespace,
        pod_name=pod_name,
        reason=reason,
        message=message,
        type=type_,
        event_uid=event_uid,
        first_seen=first_seen,
        last_seen=last_seen,
        count=count,
        extras=extras,
    )
    db.add(ev)
    db.flush()
    return ev
