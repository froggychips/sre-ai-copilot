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
        extras: Dict[str, Any] = d.extras if isinstance(d.extras, dict) else {}
        out.append({
            "name": service_name,
            "ts": d.started_at,
            "sha": d.sha,
            "repo": d.repo,
            "buildtype_id": d.buildtype_id,
            "buildtype_name": extras.get("buildtype_name") or d.buildtype_id,
            "number": d.build_number,
            "status": d.status,
            "triggered_by": d.triggered_by,
            "url": extras.get("url"),
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


def blast_radius_for(
    db: Session,
    namespace: str,
    service_name: str,
    top_n: int = 3,
) -> Dict[str, Any]:
    """Wave 7 (X, PR #71): blast radius для упавшего сервиса.

    Считает:
        * `serves_traffic` IN-edges (kind='serves_traffic', dst=svc):
          какие k8s-Service'ы маршрутят трафик на этот Deployment.
          Это «сервисные точки входа» — клиенты ходят через них.
        * `routes_to` IN-edges (kind='routes_to', dst=svc): какие
          Ingress-ресурсы натравлены на этот backend. extras.host даёт
          внешний URL.

    Возвращает `{services: [name, ...top_n], urls: [host, ...top_n],
                  services_total: int, urls_total: int}`.

    Используется в Discord embed-секции «🎯 Blast radius» (только critical).
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return {"services": [], "urls": [], "services_total": 0, "urls_total": 0}

    # serves_traffic — `kg_services` ноды (k8s Service), маршрутизирующие
    # трафик на этот Deployment. Имя ноды k8s-Service'а лежит как есть.
    serves_rows = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.dst_id == svc.id,
            ServiceEdge.kind == "serves_traffic",
        )
        .all()
    )
    services_seen: List[str] = []
    for edge in serves_rows:
        if edge.src is None:
            continue
        if edge.src.name in services_seen:
            continue
        services_seen.append(edge.src.name)

    # routes_to — Ingress synthetic nodes (`ingress:<name>`). Хост-имя
    # лежит в `extras.host`. Если host='*' (wildcard) — пропускаем,
    # для оператора оно не информативно как URL.
    routes_rows = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.dst_id == svc.id,
            ServiceEdge.kind == "routes_to",
        )
        .all()
    )
    urls_seen: List[str] = []
    for edge in routes_rows:
        extras: Dict[str, Any] = (
            edge.extras if isinstance(edge.extras, dict) else {}
        )
        host = extras.get("host")
        if not host or host == "*":
            continue
        if host in urls_seen:
            continue
        urls_seen.append(host)

    return {
        "services": services_seen[:top_n],
        "urls": urls_seen[:top_n],
        "services_total": len(services_seen),
        "urls_total": len(urls_seen),
    }


def nats_impact_for(
    db: Session,
    namespace: str,
    service_name: str,
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """Wave 7 (Z, PR #72): NATS impact — subjects + co-consumers.

    Для каждого `uses_nats` OUT-edge (src=svc, dst=NATS subject synthetic
    node) считает сколько ДРУГИХ сервисов используют этот subject
    (в любом direction). Это «impact count» — оценка broadcast-радиуса.

    `extras.direction` (pub|sub) берётся из edge на текущий сервис.

    Возвращает list[dict] (sorted by impact_count desc, max `top_n`):
        [{subject, direction, impact_count, impact_others: [(name, dir)...]}]

    Пустой если у сервиса нет NATS-edges (skip-if-empty в embed).
    Один query на subjects + один batch query на impact_others — не N
    запросов на subject.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []

    out_edges = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.src_id == svc.id,
            ServiceEdge.kind == "uses_nats",
        )
        .all()
    )
    if not out_edges:
        return []

    # Собираем subject-node IDs за один батч, чтобы посчитать ко-консьюмеров
    # одним SQL-запросом, а не N.
    subject_ids: List[int] = []
    by_subject_id: Dict[int, Dict[str, Any]] = {}
    for edge in out_edges:
        if edge.dst is None:
            continue
        sid = int(edge.dst_id)
        extras: Dict[str, Any] = (
            edge.extras if isinstance(edge.extras, dict) else {}
        )
        direction = (extras.get("direction") or "?").lower()
        subject_ids.append(sid)
        by_subject_id[sid] = {
            "subject": edge.dst.name,
            "direction": direction,
            "impact_count": 0,
            "impact_others": [],
        }

    if subject_ids:
        co_rows = (
            db.query(ServiceEdge)
            .filter(
                ServiceEdge.dst_id.in_(subject_ids),
                ServiceEdge.kind == "uses_nats",
                ServiceEdge.src_id != svc.id,
            )
            .all()
        )
        for r in co_rows:
            if r.src is None:
                continue
            entry = by_subject_id.get(r.dst_id)
            if entry is None:
                continue
            entry["impact_count"] += 1
            if len(entry["impact_others"]) < 3:
                r_extras: Dict[str, Any] = (
                    r.extras if isinstance(r.extras, dict) else {}
                )
                entry["impact_others"].append((
                    r.src.name,
                    (r_extras.get("direction") or "?").lower(),
                ))

    result = list(by_subject_id.values())
    result.sort(key=lambda x: x["impact_count"], reverse=True)
    return result[:top_n]


def pod_event_summary_for(
    db: Session,
    namespace: str,
    service_name: str,
    around: datetime,
    window_minutes: int = 60,
) -> Dict[str, Any]:
    """Wave 7 (Y, PR #70): агрегированная сводка PodEvent для второй секции.

    Берёт `kg_pod_events` в окне ±window_minutes от `around` для сервиса
    (через runtime_correlation linkage), группирует по `reason`, отдаёт
    counts. Используется в Discord embed-секции «🕒 Pod trail» (только
    critical) — даёт быстрый сигнал «5 evts: 3 OOMKilled, 2 CrashLoopBackOff».

    Возвращает `{total: int, by_reason: [(reason, count), ...desc]}`.
    Пустой dict если нет событий (skip-if-empty в embed).
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return {"total": 0, "by_reason": []}
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
        .all()
    )
    if not rows:
        return {"total": 0, "by_reason": []}

    by_reason: Dict[str, int] = {}
    total = 0
    for r in rows:
        # PodEvent.count = сколько раз k8s видел этот event (агрегация
        # за весь lifetime). Для «сколько падений было» используем count;
        # `max(1, count)` чтобы NULL/0 не схлопывали row.
        c = max(1, int(r.count or 1))
        reason_key = str(r.reason)
        by_reason[reason_key] = by_reason.get(reason_key, 0) + c
        total += c

    pairs = sorted(by_reason.items(), key=lambda kv: kv[1], reverse=True)
    return {"total": total, "by_reason": pairs}


def latest_pod_event_for(
    db: Session,
    namespace: str,
    service_name: str,
) -> Optional[Dict[str, Any]]:
    """Самое свежее `kg_pod_events`-событие для сервиса.

    Используется enrichment-ом, чтобы вытащить `pod_name` + `reason` для
    embed-полей «Pod» / «Reason». В `recent_pod_events_for` уже есть
    window-фильтр, тут нужен просто «последнее что было» без окна — для
    кейса когда window-fallback (7д) тоже пуст и хочется хоть что-то
    показать.

    Возвращает dict с {pod_name, reason, last_seen, first_seen, count,
    message, minutes_ago}. None если событий вообще нет.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return None
    row = (
        db.query(PodEvent)
        .filter(PodEvent.service_id == svc.id)
        .order_by(PodEvent.first_seen.desc())
        .first()
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    first_aware = _ensure_aware(row.first_seen)
    minutes_ago = int((now - first_aware).total_seconds() // 60)
    return {
        "pod_name": row.pod_name,
        "reason": row.reason,
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
        "count": row.count,
        "message": (row.message or "")[:200],
        "minutes_ago": minutes_ago,
    }


def current_replicas_from_kg(
    db: Session,
    namespace: str,
    service_name: str,
) -> Optional[Dict[str, Any]]:
    """Прочитать ready/desired из `kg_services.metadata_json` (если есть).

    Дешёвая попытка — read-only Service row. Если populator не пишет
    `replicas`/`ready_replicas` в metadata_json — вернёт None и caller
    может пойти в live k8s API.

    Ожидаемые ключи в metadata_json (по согласованию с populator):
        * `replicas` или `replicas_desired` — int
        * `ready_replicas` или `replicas_ready` — int

    Возвращает {ready, desired} или None.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return None
    meta: Dict[str, Any] = svc.metadata_json or {}
    if not isinstance(meta, dict):
        return None
    desired = meta.get("replicas_desired")
    if desired is None:
        desired = meta.get("replicas")
    ready = meta.get("replicas_ready")
    if ready is None:
        ready = meta.get("ready_replicas")
    if desired is None and ready is None:
        return None
    return {
        "ready": int(ready) if ready is not None else None,
        "desired": int(desired) if desired is not None else None,
    }


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
