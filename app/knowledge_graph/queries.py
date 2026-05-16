"""Графовые запросы поверх SQLAlchemy.

Все функции принимают Session — вызывающий контролирует жизненный цикл
транзакции. Возвращают обычные dict/list, а не ORM-объекты, чтобы
hypothesis/critic-агенты могли сериализовать в JSON-промпт.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.knowledge_graph.confidence import (confidence_label,
                                            confidence_score)
from app.knowledge_graph.schema import (AlertEvent, Deployment, PodEvent,
                                        Service, ServiceEdge)


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _service_by_namespace_name(
    db: Session, namespace: str, name: str
) -> Optional[Service]:
    return (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == name)
        .one_or_none()
    )


def recent_deploys_for(
    db: Session,
    namespace: str,
    service_name: str,
    before: datetime,
    lookback_minutes: int = 60,
) -> List[Dict[str, Any]]:
    """Деплои сервиса за [before - lookback, before].

    Используется RecentDeployRule, когда граф наполнен. Если populator
    не запущен и сервиса в графе нет — возвращаем []. Это эквивалентно
    «не знаем», и RecentDeployRule выдаст observed=False с явным reason.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []
    before_aware = _ensure_aware(before)
    since = before_aware - timedelta(minutes=lookback_minutes)
    rows = (
        db.query(Deployment)
        .filter(
            Deployment.service_id == svc.id,
            Deployment.started_at >= since.replace(tzinfo=None),
            Deployment.started_at <= before_aware.replace(tzinfo=None),
        )
        .order_by(Deployment.started_at.desc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for d in rows:
        delta_min = int(
            (before_aware - d.started_at.replace(tzinfo=timezone.utc)).total_seconds() // 60
        )
        out.append({
            "name": service_name,
            "ts": d.started_at,
            "sha": d.sha,
            "repo": d.repo,
            "buildtype_id": d.buildtype_id,
            "number": d.build_number,
            "status": d.status,
            "minutes_before_incident": delta_min,
        })
    return out


def upstream_of(
    db: Session,
    namespace: str,
    service_name: str,
    kinds: Optional[List[str]] = None,
    fresh_only_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Сервисы, от которых зависит данный.

    Если town `calls` auth → upstream_of(town) включает auth. Семантически
    это «если эти сервисы лягут, то текущий с большой вероятностью тоже».
    Используется UpstreamDegradedRule для поиска alert-ов на upstream.

    `fresh_only_days` (C1): если задано — фильтр edges по
    `last_seen_at >= now() - fresh_only_days`. Защита от stale-зависимостей
    (сервис убрал env-var, но edge остался в KG).
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []
    q = db.query(ServiceEdge).filter(ServiceEdge.src_id == svc.id)
    if kinds:
        q = q.filter(ServiceEdge.kind.in_(kinds))
    if fresh_only_days is not None:
        fresh_cutoff = datetime.utcnow() - timedelta(days=fresh_only_days)
        q = q.filter(ServiceEdge.last_seen_at >= fresh_cutoff)
    out: List[Dict[str, Any]] = []
    for edge in q.all():
        if edge.dst is None:
            continue
        score = confidence_score(edge.extras, edge.last_seen_at)
        out.append({
            "service": edge.dst.name,
            "namespace": edge.dst.namespace,
            "kind": edge.kind,
            "weight": edge.weight,
            "last_seen_at": edge.last_seen_at,
            "discovery_sources": (edge.extras or {}).get("discovery_sources") or [],
            "confidence_score": score,
            "confidence_label": confidence_label(score),
        })
    return out


def incidents_on(
    db: Session,
    namespace: str,
    service_name: str,
    since: datetime,
    until: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Alert-события на сервисе в окне [since, until]."""
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []
    until = until or datetime.now(timezone.utc)
    rows = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.service_id == svc.id,
            AlertEvent.fired_at >= _ensure_aware(since).replace(tzinfo=None),
            AlertEvent.fired_at <= _ensure_aware(until).replace(tzinfo=None),
        )
        .order_by(AlertEvent.fired_at.desc())
        .all()
    )
    return [
        {
            "alertname": a.alertname,
            "severity": a.severity,
            "fingerprint": a.fingerprint,
            "fired_at": a.fired_at,
            "resolved_at": a.resolved_at,
        }
        for a in rows
    ]


def nearby_alerts(
    db: Session,
    namespace: str,
    service_name: str,
    around: datetime,
    window_minutes: int = 15,
) -> List[Dict[str, Any]]:
    """Alert-ы на upstream-сервисах в окне ±window_minutes от `around`.

    Это и есть основной запрос для UpstreamDegradedRule:
        upstream = upstream_of(svc)
        for u in upstream:
            alerts += incidents_on(u, around - W, around + W)
    """
    around_aware = _ensure_aware(around)
    since = around_aware - timedelta(minutes=window_minutes)
    until = around_aware + timedelta(minutes=window_minutes)

    upstream = upstream_of(db, namespace, service_name)
    out: List[Dict[str, Any]] = []
    for u in upstream:
        alerts = incidents_on(db, u["namespace"], u["service"], since, until)
        for a in alerts:
            fired_aware = _ensure_aware(a["fired_at"])
            delta_min = int((around_aware - fired_aware).total_seconds() // 60)
            out.append({
                "service": u["service"],
                "namespace": u["namespace"],
                "alertname": a["alertname"],
                "severity": a["severity"],
                "fired_at": a["fired_at"],
                "minutes_before": delta_min,
                "edge_kind": u["kind"],
            })
    return out


def recent_pod_events_for(
    db: Session,
    namespace: str,
    service_name: str,
    around: datetime,
    window_minutes: int = 30,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """A4: PodEvent для сервиса в окне [around-window, around+window].

    Используется в alert_enrichment чтобы дополнить embed строкой типа
    "OOMKilled ×3 (last 12m ago)" — k8s-level signal, который AlertManager
    upstream-rule может не отразить.

    Возвращает [{reason, pod_name, first_seen, last_seen, count,
                  minutes_before, message}] по убыванию first_seen.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []
    around_aware = _ensure_aware(around)
    since = around_aware - timedelta(minutes=window_minutes)
    until = around_aware + timedelta(minutes=window_minutes)

    rows = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == svc.id,
            PodEvent.first_seen >= since.replace(tzinfo=None),
            PodEvent.first_seen <= until.replace(tzinfo=None),
        )
        .order_by(PodEvent.first_seen.desc())
        .limit(limit)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        first_aware = _ensure_aware(r.first_seen)
        delta_min = int((around_aware - first_aware).total_seconds() // 60)
        out.append({
            "reason": r.reason,
            "pod_name": r.pod_name,
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
            "count": r.count,
            "minutes_before": delta_min,
            "message": (r.message or "")[:200],
        })
    return out
