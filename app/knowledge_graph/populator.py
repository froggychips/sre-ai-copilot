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

from app.knowledge_graph.contract import UQ_KG_SERVICE_NS_NAME_KIND
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, AlertEvent,
                                        Deployment, PodEvent, Service,
                                        ServiceEdge)

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
    node_kind: str = NODE_KIND_SERVICE,
    stale_class: Optional[str] = None,
) -> Service:
    """Idempotent upsert узла графа — ЕДИНСТВЕННЫЙ путь записи kg_services.

    До 14.08.2026 путей было два: этот и `kg_sync._upsert_service_pg` со своей
    копией `ON CONFLICT`. Копии разъехались при переходе на трёхколоночный ключ
    (#245): вторая ссылалась на удалённый констрейнт, kg_topology_sync падал на
    каждом namespace, services=0 сутки. Теперь второй путь — тонкая обёртка
    вокруг этого, а имя констрейнта берётся из contract.

    `node_kind` различает k8s Service, workload (Deployment/StatefulSet/
    DaemonSet) и synthetic ingress-узлы. Дефолт 'service' — так все прежние
    вызовы сохраняют поведение, а узлы workload заводятся только там, где
    это явно нужно (синк топологии).

    На PostgreSQL использует INSERT ON CONFLICT DO UPDATE — атомарно, без
    race condition при параллельных worker'ах.
    На других диалектах (SQLite в тестах) — старый SELECT+INSERT.
    """
    if _is_postgresql(db):
        return _upsert_service_pg(
            db, namespace, name, team_owner, metadata, synthetic, node_kind,
            stale_class,
        )
    return _upsert_service_fallback(
        db, namespace, name, team_owner, metadata, synthetic, node_kind,
        stale_class,
    )


def _upsert_service_pg(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str],
    metadata: Optional[Dict[str, Any]],
    synthetic: Optional[bool],
    node_kind: str = NODE_KIND_SERVICE,
    stale_class: Optional[str] = None,
) -> Service:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.utcnow()
    values: Dict[str, Any] = {
        "namespace": namespace,
        "name": name,
        "node_kind": node_kind,
        "team_owner": team_owner,
        "metadata_json": metadata,
        "synthetic": bool(synthetic) if synthetic is not None else False,
        "stale_class": stale_class,
        "created_at": now,
        "updated_at": now,
    }
    set_clause: Dict[str, Any] = {"updated_at": now}
    if team_owner:
        set_clause["team_owner"] = team_owner
    # stale_class обновляем только когда вызывающий его посчитал: None здесь
    # означает «не знаю», а не «сбросить в NULL» (иначе topology-sync стирал бы
    # expected_stale, выставленный другим источником).
    if stale_class is not None:
        set_clause["stale_class"] = stale_class
    # metadata_json НЕ кладём в set_clause: полный overwrite стирал ключи
    # других источников (auto_populator пишет app/component/version,
    # drift_cleanup — drift_marked_at, topology-sync — k8s_service).
    # Merge делаем в Python после upsert-а (см. ниже) — каждый источник
    # владеет своими ключами, чужие не трогает.
    if synthetic is not None:
        set_clause["synthetic"] = bool(synthetic)

    stmt = (
        pg_insert(Service.__table__)
        .values(**values)
        .on_conflict_do_update(
            constraint=UQ_KG_SERVICE_NS_NAME_KIND,
            set_=set_clause,
        )
        .returning(Service.__table__.c.id)
    )
    db.execute(stmt)
    db.flush()
    logger.info("kg.service_upserted", namespace=namespace, name=name, node_kind=node_kind)
    # populate_existing(): upsert шёл через Core — identity-map мог держать
    # stale-инстанс, перечитываем поверх (та же история что в record_alert_event).
    svc = (
        db.query(Service)
        .filter_by(namespace=namespace, name=name, node_kind=node_kind)
        .populate_existing()
        .one()
    )
    if metadata is not None:
        existing_meta: Dict[str, Any] = (
            svc.metadata_json if isinstance(svc.metadata_json, dict) else {}
        )
        merged = dict(existing_meta)
        merged.update(metadata)
        if merged != existing_meta:
            svc.metadata_json = merged
            db.flush()
    return svc


def _upsert_service_fallback(
    db: Session,
    namespace: str,
    name: str,
    team_owner: Optional[str],
    metadata: Optional[Dict[str, Any]],
    synthetic: Optional[bool],
    node_kind: str = NODE_KIND_SERVICE,
    stale_class: Optional[str] = None,
) -> Service:
    svc = (
        db.query(Service)
        .filter(
            Service.namespace == namespace,
            Service.name == name,
            Service.node_kind == node_kind,
        )
        .one_or_none()
    )
    if svc is None:
        svc = Service(
            namespace=namespace,
            name=name,
            node_kind=node_kind,
            team_owner=team_owner,
            metadata_json=metadata,
            synthetic=bool(synthetic) if synthetic is not None else False,
            stale_class=stale_class,
        )
        db.add(svc)
        db.flush()
        logger.info("kg.service_created", namespace=namespace, name=name)
    else:
        changed = False
        if team_owner and svc.team_owner != team_owner:
            svc.team_owner = team_owner
            changed = True
        # None = «вызывающий не считал», а не «сбросить» — зеркалит PG-путь.
        if stale_class is not None and svc.stale_class != stale_class:
            svc.stale_class = stale_class
            changed = True
        if metadata is not None:
            # Merge, не overwrite — сохраняем ключи других источников
            # (зеркалит PG-путь).
            existing_meta: Dict[str, Any] = (
                svc.metadata_json if isinstance(svc.metadata_json, dict) else {}
            )
            merged = dict(existing_meta)
            merged.update(metadata)
            if merged != existing_meta:
                svc.metadata_json = merged
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
    direction: str = "",
) -> ServiceEdge:
    """Idempotent upsert по (src_id, dst_id, kind, direction).

    На PostgreSQL — INSERT ON CONFLICT, исключает race condition.
    `extras` (JSON): discovery_sources и confidence — merge, не overwrite.

    `direction` — часть идентичности ребра для kinds где направление
    различает РАЗНЫЕ рёбра (uses_nats: `pub` / `sub` сосуществуют).
    Для остальных kinds — оставить дефолт "" (поведение как раньше).
    """
    if _is_postgresql(db):
        return _upsert_edge_pg(
            db, src, dst, kind, weight, discovered_by, extras, direction,
        )
    return _upsert_edge_fallback(
        db, src, dst, kind, weight, discovered_by, extras, direction,
    )


def _upsert_edge_pg(
    db: Session,
    src: Service,
    dst: Service,
    kind: str,
    weight: int,
    discovered_by: Optional[str],
    extras: Optional[Dict[str, Any]],
    direction: str = "",
) -> ServiceEdge:
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.utcnow()
    initial_extras = dict(extras or {})
    if discovered_by:
        initial_extras.setdefault("discovery_sources", [discovered_by])

    stmt = (
        pg_insert(ServiceEdge.__table__)
        .values(
            src_id=src.id, dst_id=dst.id, kind=kind, direction=direction or "",
            weight=weight, discovered_by=discovered_by,
            extras=initial_extras or None, last_seen_at=now,
        )
    )
    # KG H5: НЕ понижаем weight. Env-discovered рёбра всегда приходят с
    # weight=1 (дефолт upsert_edge). Если runtime/traffic-источник проставил
    # «жирность» (доля трафика, важность), следующий env-sync затёр бы её в 1.
    # GREATEST(existing, excluded) — монотонный апдейт: weight только растёт.
    stmt = stmt.on_conflict_do_update(
        constraint="uq_kg_edge_src_dst_kind_direction",
        set_={
            "last_seen_at": now,
            "weight": func.greatest(
                ServiceEdge.__table__.c.weight, stmt.excluded.weight
            ),
            **({"discovered_by": discovered_by} if discovered_by else {}),
        },
    )
    db.execute(stmt)
    db.flush()

    # populate_existing(): upsert шёл через Core — identity-map мог держать
    # stale-инстанс (weight/last_seen_at с прошлого вызова). Перечитываем
    # строку поверх него (та же починка, что в record_alert_event).
    edge = (
        db.query(ServiceEdge)
        .filter_by(
            src_id=src.id, dst_id=dst.id, kind=kind, direction=direction or "",
        )
        .populate_existing()
        .one()
    )
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
    direction: str = "",
) -> ServiceEdge:
    edge = (
        db.query(ServiceEdge)
        .filter(ServiceEdge.src_id == src.id, ServiceEdge.dst_id == dst.id,
                ServiceEdge.kind == kind,
                ServiceEdge.direction == (direction or ""))
        .one_or_none()
    )
    now = datetime.utcnow()
    initial_extras = dict(extras or {})
    if discovered_by:
        initial_extras.setdefault("discovery_sources", [discovered_by])

    if edge is None:
        edge = ServiceEdge(
            src_id=src.id, dst_id=dst.id, kind=kind, weight=weight,
            direction=direction or "",
            discovered_by=discovered_by, extras=initial_extras or None,
            last_seen_at=now,
        )
        db.add(edge)
        db.flush()
    else:
        edge.last_seen_at = now
        # KG H5: weight монотонно растёт — env-sync (weight=1) не понижает
        # «жирность», проставленную runtime/traffic-источником. Зеркалит
        # GREATEST(existing, excluded) из PG-пути.
        if weight > edge.weight:
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
    # если появляется в нескольких инцидентах. Раньше был check-then-insert —
    # конкурентные вызовы (beat task tc_deploys_to_kg + incident pipeline)
    # проскакивали между SELECT и INSERT и плодили дубли, раздувая
    # deploy_count / deploy_failure_pct в signal_aggregates. Теперь —
    # атомарный INSERT ON CONFLICT по uq_kg_deploy_service_build.
    if buildtype_id and build_number:
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        tbl = Deployment.__table__
        stmt = pg_insert(tbl).values(
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
        # На конфликт — прежняя семантика dedup-а: существующая строка
        # остаётся как есть, только sha бэкфиллится если раньше был NULL.
        stmt = stmt.on_conflict_do_update(
            index_elements=["service_id", "buildtype_id", "build_number"],
            set_={"sha": func.coalesce(tbl.c.sha, stmt.excluded.sha)},
        )
        db.execute(stmt)
        db.flush()
        # populate_existing() — та же защита от stale identity-map, что и
        # в record_alert_event / _upsert_edge_pg.
        return (
            db.query(Deployment)
            .filter(
                Deployment.service_id == service.id,
                Deployment.buildtype_id == buildtype_id,
                Deployment.build_number == build_number,
            )
            .populate_existing()
            .one()
        )

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
    """Идемпотентно по fingerprint через INSERT ON CONFLICT — race-safe при
    нескольких репликах воркера (раньше check-then-insert ловил гонку, а с
    восстановленным UNIQUE(fingerprint) — ещё и IntegrityError). На конфликт:
    severity/raw обновляются только если переданы (COALESCE), last_notified_at
    всегда; service_id/incident_id не трогаем.

    Re-fire закрытого алерта: если существующая строка уже resolved
    (resolved_at NOT NULL), повторный fire с тем же fingerprint — это НОВЫЙ
    инцидент того же источника (external_probe шлёт стабильный
    `external_probe:{host}`). Тогда resolved_at сбрасывается в NULL, fired_at
    обновляется на новый. Раньше строка навсегда оставалась «resolved» —
    health_score / stuck_alerts / RCA не видели открытый алерт, а
    resolve-путь не находил что закрывать. Дедуп ЕЩЁ ОТКРЫТОГО алерта
    (resolved_at IS NULL) не ломаем: fired_at оригинала сохраняется.

    Без fingerprint идемпотентность невозможна — обычный insert.
    """
    now = datetime.utcnow()
    if not fingerprint:
        ev = AlertEvent(
            service_id=service.id if service else None,
            alertname=alertname,
            severity=severity,
            fingerprint=None,
            fired_at=fired_at,
            last_notified_at=now,
            incident_id=incident_id,
            raw=raw,
        )
        db.add(ev)
        db.flush()
        return ev

    from sqlalchemy import case, null
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    tbl = AlertEvent.__table__
    stmt = pg_insert(tbl).values(
        service_id=service.id if service else None,
        alertname=alertname,
        severity=severity,
        fingerprint=fingerprint,
        fired_at=fired_at,
        last_notified_at=now,
        incident_id=incident_id,
        raw=raw,
    )
    # На конфликт обновляем только переданные поля (как было до upsert):
    # severity/raw трогаем лишь если непустые, last_notified_at — всегда.
    # COALESCE здесь не годится: JSON-колонка сериализует None как JSON
    # `null` (не SQL NULL), и COALESCE(EXCLUDED.raw, ...) затёр бы raw.
    #
    # fired_at / resolved_at — CASE по состоянию существующей строки:
    #   * строка resolved (resolved_at NOT NULL) → genuine re-fire: fired_at
    #     берём новый (EXCLUDED), resolved_at сбрасываем в NULL;
    #   * строка ещё открыта → дедуп ongoing-алерта: оба поля не трогаем
    #     (fired_at оригинала сохраняется).
    set_clause: Dict[str, Any] = {
        "last_notified_at": now,
        "fired_at": case(
            (tbl.c.resolved_at.isnot(None), stmt.excluded.fired_at),
            else_=tbl.c.fired_at,
        ),
        "resolved_at": case(
            (tbl.c.resolved_at.isnot(None), null()),
            else_=tbl.c.resolved_at,
        ),
    }
    if severity:
        set_clause["severity"] = severity
    if raw is not None:
        set_clause["raw"] = raw
    stmt = stmt.on_conflict_do_update(
        index_elements=["fingerprint"],
        set_=set_clause,
    )
    db.execute(stmt)
    db.flush()
    # populate_existing(): upsert шёл через Core, поэтому identity-map мог
    # держать stale-инстанс (severity/raw с прошлого вызова). Перечитываем
    # строку поверх него, иначе вернём устаревшие атрибуты.
    return (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == fingerprint)
        .populate_existing()
        .one()
    )


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
        # Бэкфилл атрибуции: если первый sync сохранил service_id=NULL
        # (сервиса ещё не было в KG), а сейчас сервис резолвится —
        # до-проставляем его. Уже непустой service_id не перетираем.
        if existing.service_id is None and service is not None:
            existing.service_id = service.id
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
