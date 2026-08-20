"""Графовые запросы поверх SQLAlchemy.

Все функции принимают Session — вызывающий контролирует жизненный цикл
транзакции. Возвращают обычные dict/list, а не ORM-объекты, чтобы
hypothesis/critic-агенты могли сериализовать в JSON-промпт.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from app.core.timeutil import ensure_aware, ensure_naive
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.knowledge_graph.confidence import (confidence_label,
                                            confidence_score)
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        AlertEvent, Deployment,
                                        IngressObservation, LogObservation,
                                        PodEvent, Service, ServiceEdge)






# ── ns-level deploy attribution: как не считать один билд K×2 раза ──────────
#
# `tc_deploys_to_kg` (app/workers/tasks.py) броадкастит ОДИН TC-билд на
# КАЖДЫЙ non-synthetic узел namespace — включая workload-дубль одноимённого
# сервиса (contract 2.4: Service `auth` и workload `auth` — разные строки
# kg_services). Джойн по одному namespace поэтому возвращает один билд
# K×2 раза: `limit` съедался копиями одного билда, а «60 deploys» в эмбеде
# означали один билд, разосланный 30 сервисам. Честны были только
# `distinct_builds`.
#
# Лечим двумя фильтрами:
#   1. считаем деплои только по service-узлам — workload-узел заводится
#      топологическим синком РЯДОМ с одноимённым service-узлом того же ns
#      (k8s_topology_resources_sync._sync_one_service), а kg_sync держит
#      service-узел для каждого k8s Deployment ns, поэтому ни один билд из
#      ns-broadcast так не теряется;
#   2. схлопываем fan-out по (namespace, buildtype_id, build_number) —
#      «сколько деплоев» становится «сколько билдов реально каталось в ns».
#
# Потолок строк под дедуп: один билд лежит в K строках, поэтому `limit`
# уникальных билдов может оказаться за сотнями строк. 2000 хватает на
# ~40 билдов при фан-ауте 50 и держит запрос ограниченным.
_NS_DEPLOY_SCAN_CAP = 2000


def _deploy_dedup_key(deploy: Deployment, namespace: str) -> tuple:
    """Ключ схлопывания ns-broadcast: один TC-билд = одна строка на namespace.

    Записи без build-инфо (`record_deployment` без TC-контекста: только
    `started_at`) не идентифицируемы — общий ключ схлопнул бы РАЗНЫЕ деплои
    в один, поэтому для них дедуп выключен (ключ = id строки).
    """
    if not deploy.buildtype_id and not deploy.build_number:
        return ("deploy_id", int(deploy.id))
    return (namespace, deploy.buildtype_id, deploy.build_number)


def _service_by_namespace_name(
    db: Session, namespace: str, name: str
) -> Optional[Service]:
    return (
        db.query(Service)
        .filter(
            Service.namespace == namespace,
            Service.name == name,
            # без node_kind запрос стал бы неоднозначным: одноимённый
            # workload-узел даёт MultipleResultsFound на .one_or_none()
            Service.node_kind == NODE_KIND_SERVICE,
        )
        .one_or_none()
    )


def services_by_stale_class(
    db: Session,
    stale_class: str,
    *,
    namespace: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Service]:
    """Сервисы с заданным ``kg_services.stale_class``.

    Значения: ``active`` | ``expected_stale`` | ``suspicious_stale``.
    Заполняется в ``kg_sync.sync_namespace`` (KG Coverage #4).

    Используется stats-digest, dashboards и owner-routing'ом alert-ов
    (suspicious_stale деплоймент роняет alert → assignee = team_owner kg_svc).
    """
    q = db.query(Service).filter(Service.stale_class == stale_class)
    if namespace is not None:
        q = q.filter(Service.namespace == namespace)
    q = q.order_by(Service.namespace, Service.name)
    if limit is not None:
        q = q.limit(limit)
    return q.all()


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
    before_aware = ensure_aware(before)
    since = before_aware - timedelta(minutes=lookback_minutes)
    rows = (
        db.query(Deployment)
        .filter(
            Deployment.service_id == svc.id,
            Deployment.started_at >= ensure_naive(since),
            Deployment.started_at <= ensure_naive(before_aware),
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


def recent_deploys_for_namespaces(
    db: Session,
    namespaces: List[str],
    before: datetime,
    lookback_minutes: int = 60,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Деплои ЛЮБОГО сервиса в указанных namespace за [before-lookback, before].

    NS-level fallback для deploy attribution: алерты-агрегаты по namespace
    (PreprodRestartsSpike, PreprodEndpointDown) не резолвятся в kg_services →
    сервисный `recent_deploys_for` бессилен. Но triage-вопрос on-call
    «это деплой или нет» отвечается и без сервиса: был ли ХОТЬ ОДИН deploy
    в namespace прямо перед алертом. kg_deployments не хранит namespace —
    идём через join на kg_services.

    `limit` — количество РАЗНЫХ билдов (см. `_deploy_dedup_key`): без дедупа
    ns-broadcast'а пять слотов забирали пять копий одного билда, и эмбед
    показывал «5 деплоев», которых не было.
    """
    if not namespaces:
        return []
    before_aware = ensure_aware(before)
    since = before_aware - timedelta(minutes=lookback_minutes)
    rows = (
        db.query(Deployment, Service)
        .join(Service, Deployment.service_id == Service.id)
        .filter(
            Service.namespace.in_(namespaces),
            Service.node_kind == NODE_KIND_SERVICE,
            Deployment.started_at >= ensure_naive(since),
            Deployment.started_at <= ensure_naive(before_aware),
        )
        .order_by(Deployment.started_at.desc())
        .limit(_NS_DEPLOY_SCAN_CAP)
        .all()
    )
    out: List[Dict[str, Any]] = []
    seen_builds: set = set()
    for d, svc in rows:
        bkey = _deploy_dedup_key(d, svc.namespace)
        if bkey in seen_builds:
            continue
        seen_builds.add(bkey)
        delta_min = int(
            (before_aware - d.started_at.replace(tzinfo=timezone.utc)).total_seconds() // 60
        )
        extras: Dict[str, Any] = d.extras if isinstance(d.extras, dict) else {}
        out.append({
            "name": svc.name,
            "namespace": svc.namespace,
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
        if len(out) >= limit:
            break
    return out


def deploy_stream_freshness(
    db: Session,
    *,
    before: Optional[datetime] = None,
    stale_after_hours: int = 6,
) -> Dict[str, Any]:
    """Жив ли поток деплоев вообще: `{last_at, age_hours, stale}`.

    Отвечает на вопрос, который НЕ отвечает пустой `recent_deploys_for_namespaces`:
    «в этом ns деплоя не было» и «мы уже сутки ничего не пишем в kg_deployments» —
    это разные факты, а выглядели одинаково (пустой список).

    Инцидент 2026-08-11: `tc_deploys_to_kg` перестал писать 10.08, и на алерте
    ProdRestartsSpike, прилетевшем через 20 секунд после прод-раскатки, embed
    уверенно сообщил «деплоев не было — вряд ли связано с деплоем». Триаж
    получил ложноотрицательный вердикт в самый неудачный момент.

    `stale=True` означает «источнику нельзя верить», а не «деплоя не было»:
    вызывающий обязан выбрать честную формулировку. Глобально по таблице (без
    ns-фильтра) — интересует здоровье пайплайна, а не активность конкретного ns.
    Порог 6h умышленно щедрый: ночью деплоев штатно нет, ложный «stale» хуже
    молчания.
    """
    before_aware = ensure_aware(before or datetime.now(timezone.utc))
    last_at = (
        db.query(func.max(Deployment.started_at))
        .filter(Deployment.started_at <= ensure_naive(before_aware))
        .scalar()
    )
    if last_at is None:
        return {"last_at": None, "age_hours": None, "stale": True}
    age_hours = (
        before_aware - last_at.replace(tzinfo=timezone.utc)
    ).total_seconds() / 3600.0
    return {
        "last_at": last_at,
        "age_hours": round(age_hours, 1),
        "stale": age_hours > stale_after_hours,
    }


def cluster_deploy_activity(
    db: Session,
    *,
    sibling_prefixes: List[str],
    exclude_namespace: str,
    before: datetime,
    lookback_minutes: int = 60,
    sample_limit: int = 3,
) -> Dict[str, Any]:
    """Агрегат deploy-активности в СОСЕДНИХ ns одного кластера за окно.

    Cross-namespace collateral (инцидент ProdEndpointDown 2026-06-15):
    bulk-rollout в соседних namespace одного физического кластера роняет
    соседей через image-pull/CRI-pressure, а per-namespace deploy
    attribution это не видит (в самом ns алерта деплоя нет). Эта функция
    отвечает на вопрос «а каталось ли что-то рядом, на том же железе».

    `sibling_prefixes` — префиксы ns кластера (Service.namespace LIKE
    'prefix%'); `exclude_namespace` — namespace алерта, исключается, чтобы
    не дублировать его собственную (пустую) атрибуцию. kg_deployments не
    хранит namespace — идём через join на kg_services.

    Возвращает `{}` если активности нет, иначе:
      total_deploys, distinct_builds, earliest_minutes_before,
      namespaces: [{namespace, deploys}, ...]  (desc by deploys),
      sample_builds: [{buildtype_name, number, triggered_by, namespace,
                       minutes_before_incident}, ...]  (ближайшие по времени).

    `total_deploys` / `namespaces[].deploys` считаются в РАЗНЫХ билдах на
    namespace, а не в строках kg_deployments: ns-broadcast раздаёт один билд
    всем узлам ns, и построчный счёт врал в десятки раз («60 deploys» вместо
    «1 билд»). Один билд, прокатившийся по трём соседним ns, даёт
    total_deploys=3 при distinct_builds=1 — это и есть bulk-rollout.
    """
    prefixes = [p for p in (sibling_prefixes or []) if p]
    if not prefixes:
        return {}
    before_aware = ensure_aware(before)
    since = before_aware - timedelta(minutes=lookback_minutes)
    rows = (
        db.query(Deployment, Service)
        .join(Service, Deployment.service_id == Service.id)
        .filter(
            or_(*[Service.namespace.like(f"{p}%") for p in prefixes]),
            Service.namespace != exclude_namespace,
            Service.node_kind == NODE_KIND_SERVICE,
            Deployment.started_at >= ensure_naive(since),
            Deployment.started_at <= ensure_naive(before_aware),
        )
        .order_by(Deployment.started_at.desc())
        .all()
    )
    if not rows:
        return {}

    per_ns: Dict[str, int] = {}
    distinct_builds = set()
    enriched: List[Dict[str, Any]] = []
    seen_ns_builds: set = set()
    for d, svc in rows:
        bkey = _deploy_dedup_key(d, svc.namespace)
        if bkey in seen_ns_builds:
            continue
        seen_ns_builds.add(bkey)
        per_ns[svc.namespace] = per_ns.get(svc.namespace, 0) + 1
        distinct_builds.add((d.buildtype_id, d.build_number))
        delta_min = int(
            (before_aware - d.started_at.replace(tzinfo=timezone.utc)).total_seconds() // 60
        )
        extras: Dict[str, Any] = d.extras if isinstance(d.extras, dict) else {}
        enriched.append({
            "namespace": svc.namespace,
            "buildtype_id": d.buildtype_id,
            "buildtype_name": extras.get("buildtype_name") or d.buildtype_id,
            "number": d.build_number,
            "triggered_by": d.triggered_by,
            "minutes_before_incident": delta_min,
        })

    # sample_builds — ближайшие к алерту, dedup по (buildtype, number) уже
    # БЕЗ namespace: один билд, прокатившийся по трём соседним ns, в примерах
    # интересен один раз (в total_deploys он честно лежит тремя ns-деплоями).
    enriched.sort(key=lambda r: r["minutes_before_incident"])
    sample_builds: List[Dict[str, Any]] = []
    seen_sample_builds = set()
    for r in enriched:
        bkey = (r["buildtype_id"], r["number"])
        if bkey in seen_sample_builds:
            continue
        seen_sample_builds.add(bkey)
        sample_builds.append(r)
        if len(sample_builds) >= sample_limit:
            break

    return {
        "total_deploys": len(enriched),
        "distinct_builds": len(distinct_builds),
        "earliest_minutes_before": min(r["minutes_before_incident"] for r in enriched),
        "namespaces": [
            {"namespace": ns, "deploys": n}
            for ns, n in sorted(per_ns.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "sample_builds": sample_builds,
    }


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
            AlertEvent.fired_at >= ensure_naive(since),
            AlertEvent.fired_at <= ensure_naive(until),
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
    around_aware = ensure_aware(around)
    since = around_aware - timedelta(minutes=window_minutes)
    until = around_aware + timedelta(minutes=window_minutes)

    upstream = upstream_of(db, namespace, service_name)
    out: List[Dict[str, Any]] = []
    for u in upstream:
        alerts = incidents_on(db, u["namespace"], u["service"], since, until)
        for a in alerts:
            fired_aware = ensure_aware(a["fired_at"])
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
    min_confidence: float = 0.0,
) -> Dict[str, Any]:
    """Wave 7 (X, PR #71): blast radius для упавшего сервиса.

    Считает:
        * `serves_traffic` IN-edges (kind='serves_traffic', dst=workload-узлы
          сервиса): какие k8s-Service'ы маршрутят трафик на этот Deployment.
          Это «сервисные точки входа» — клиенты ходят через них.
        * `routes_to` IN-edges (kind='routes_to', dst=svc): какие
          Ingress-ресурсы натравлены на этот backend. extras.host даёт
          внешний URL.

    Возвращает `{services: [name, ...top_n], urls: [host, ...top_n],
                  services_total: int, urls_total: int}` плюс
    `services_detailed` / `urls_detailed` — те же элементы с достоверностью
    ребра, и `min_confidence_seen` — худшая достоверность в выдаче.

    **Достоверность.** До 15.08.2026 blast radius был единственным публичным
    запросом графа, который не отдавал её вовсе: `upstream_of` возвращает
    `confidence_score` с 2026-06, а здесь список имён выглядел одинаково
    уверенно независимо от того, прочитано ребро из k8s-манифеста или
    угадано. Между тем это самый дорогой вопрос к графу — «кого заденет», —
    и цена ошибки в нём выше, чем где-либо ещё.

    `min_confidence` фильтрует выдачу по порогу: 0.85 оставит только
    прочитанное из k8s, 0.0 (по умолчанию) — всё. В инциденте вопрос «что
    ТОЧНО заденет» и «что может задеть» — разные вопросы.

    Используется в Discord embed-секции «🎯 Blast radius» (только critical).
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return {"services": [], "urls": [], "services_total": 0, "urls_total": 0}

    # serves_traffic — `kg_services` ноды (k8s Service), маршрутизирующие
    # трафик на этот Deployment. Producer (k8s_topology_resources_sync) пишет
    # ребро src=Service-узел → dst=WORKLOAD-узел, а `svc` выше резолвится с
    # node_kind='service' — фильтр `dst_id == svc.id` не матчил НИКОГДА, и
    # секция «Blast radius» в critical-embed молча пустовала. Поэтому dst
    # ищем среди workload-узлов этого сервиса (same namespace+name); svc.id
    # оставлен в списке ради legacy-рёбер, записанных до node_kind.
    workload_ids: List[int] = [
        wid
        for (wid,) in db.query(Service.id)
        .filter(
            Service.namespace == namespace,
            Service.name == service_name,
            Service.node_kind == NODE_KIND_WORKLOAD,
        )
        .all()
    ]
    serves_rows = (
        db.query(ServiceEdge)
        .filter(
            ServiceEdge.dst_id.in_([*workload_ids, svc.id]),
            ServiceEdge.kind == "serves_traffic",
        )
        .all()
    )
    services_seen: List[str] = []
    services_detailed: List[Dict[str, Any]] = []
    scores_seen: List[float] = []
    for edge in serves_rows:
        if edge.src is None:
            continue
        if edge.src.name in services_seen:
            continue
        score = confidence_score(edge.extras, edge.last_seen_at)
        if score < min_confidence:
            continue
        services_seen.append(edge.src.name)
        scores_seen.append(score)
        services_detailed.append({
            "name": edge.src.name,
            "namespace": edge.src.namespace,
            "confidence_score": score,
            "confidence_label": confidence_label(score),
        })

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
    urls_detailed: List[Dict[str, Any]] = []
    for edge in routes_rows:
        extras: Dict[str, Any] = (
            edge.extras if isinstance(edge.extras, dict) else {}
        )
        host = extras.get("host")
        if not host or host == "*":
            continue
        if host in urls_seen:
            continue
        score = confidence_score(edge.extras, edge.last_seen_at)
        if score < min_confidence:
            continue
        urls_seen.append(host)
        scores_seen.append(score)
        urls_detailed.append({
            "host": host,
            "confidence_score": score,
            "confidence_label": confidence_label(score),
        })

    return {
        # Плоские списки оставлены как были: на них смотрит Discord-embed,
        # и ломать формат ради нового поля незачем.
        "services": services_seen[:top_n],
        "urls": urls_seen[:top_n],
        "services_total": len(services_seen),
        "urls_total": len(urls_seen),
        "services_detailed": services_detailed[:top_n],
        "urls_detailed": urls_detailed[:top_n],
        # Худшая достоверность в выдаче: по ней потребитель решает, стоит ли
        # вообще показывать ответ как надёжный. None — показывать нечего.
        "min_confidence_seen": min(scores_seen) if scores_seen else None,
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
    # одним SQL-запросом, а не N. Ключ — (subject_id, direction): после
    # фикса pub/sub-схлопывания сервис может иметь ДВА uses_nats-ребра на
    # один subject (pub и sub) — обе строки выводим отдельно.
    subject_ids: List[int] = []
    by_subject_dir: Dict[tuple, Dict[str, Any]] = {}
    for edge in out_edges:
        if edge.dst is None:
            continue
        sid = int(edge.dst_id)
        extras: Dict[str, Any] = (
            edge.extras if isinstance(edge.extras, dict) else {}
        )
        # direction: сначала колонка (новая идентичность), fallback extras
        # для legacy-рёбер, записанных до миграции.
        direction = (
            getattr(edge, "direction", "") or extras.get("direction") or "?"
        ).lower()
        subject_ids.append(sid)
        by_subject_dir[(sid, direction)] = {
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
        # Ко-сервис с pub И sub рёбрами считаем один раз per subject
        # (как до фикса, когда ребро было одно).
        seen_co: set = set()
        for r in co_rows:
            if r.src is None:
                continue
            if (r.dst_id, r.src_id) in seen_co:
                continue
            seen_co.add((r.dst_id, r.src_id))
            r_extras: Dict[str, Any] = (
                r.extras if isinstance(r.extras, dict) else {}
            )
            r_dir = (
                getattr(r, "direction", "") or r_extras.get("direction") or "?"
            ).lower()
            for (sid, _dir), entry in by_subject_dir.items():
                if sid != r.dst_id:
                    continue
                entry["impact_count"] += 1
                if len(entry["impact_others"]) < 3:
                    entry["impact_others"].append((r.src.name, r_dir))

    result = list(by_subject_dir.values())
    result.sort(key=lambda x: x["impact_count"], reverse=True)
    return result[:top_n]


# ── PodEvent: окно по ПЕРЕСЕЧЕНИЮ, а не по first_seen ──────────────────────
#
# k8s агрегирует повторы одного и того же события в ОДНУ запись: растёт
# `count` и `lastTimestamp`, а `firstTimestamp` остаётся моментом первого
# срабатывания. У сервиса, крашащегося неделю, BackOff лежит одной строкой с
# first_seen неделю назад и живым last_seen. Фильтр `first_seen BETWEEN
# since AND until` такую строку выбрасывал — и секция «🕒 Pod trail» в
# critical-эмбеде показывала `total=0` для сервиса в хроническом crashloop,
# то есть ровно там, где она нужнее всего.
#
# Правильный предикат — пересечение интервала события [first_seen,
# last_seen] с окном [since, until]: событие «идёт» в окне, если началось не
# позже конца окна и последний раз виделось не раньше начала. `last_seen`
# nullable (старые строки до k8s_events_sync) — coalesce к first_seen
# сохраняет прежнее поведение для них.
def _pod_event_last_activity():
    """SQL-выражение «последняя активность события»."""
    return func.coalesce(PodEvent.last_seen, PodEvent.first_seen)


def _pod_event_in_window(since: datetime, until: datetime):
    """Предикат пересечения события с окном [since, until] (naive UTC)."""
    return and_(
        PodEvent.first_seen <= ensure_naive(until),
        _pod_event_last_activity() >= ensure_naive(since),
    )


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

    Окно матчится ПЕРЕСЕЧЕНИЕМ интервала события [first_seen, last_seen] с
    [around±window], а не только по `first_seen` — см. `_pod_event_in_window`.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return {"total": 0, "by_reason": []}
    around_aware = ensure_aware(around)
    since = around_aware - timedelta(minutes=window_minutes)
    until = around_aware + timedelta(minutes=window_minutes)

    rows = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == svc.id,
            _pod_event_in_window(since, until),
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
    first_aware = ensure_aware(row.first_seen)
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
                  minutes_before, minutes_since_last, message}] по убыванию
    последней активности.

    Окно — пересечение [first_seen, last_seen] с [around±window] (см.
    `_pod_event_in_window`): у хронического crashloop first_seen лежит вне
    любого окна при живом last_seen. По той же причине сортировка идёт по
    последней активности, а не по first_seen: иначе `limit` отдавал бы
    свежие мелочи, а идущий неделю BackOff уходил в хвост.
    `minutes_before` считается от first_seen («за сколько минут до алерта
    событие НАЧАЛОСЬ»), `minutes_since_last` — от последней активности.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return []
    around_aware = ensure_aware(around)
    since = around_aware - timedelta(minutes=window_minutes)
    until = around_aware + timedelta(minutes=window_minutes)

    rows = (
        db.query(PodEvent)
        .filter(
            PodEvent.service_id == svc.id,
            _pod_event_in_window(since, until),
        )
        .order_by(_pod_event_last_activity().desc())
        .limit(limit)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        first_aware = ensure_aware(r.first_seen)
        last_aware = ensure_aware(r.last_seen or r.first_seen)
        delta_min = int((around_aware - first_aware).total_seconds() // 60)
        out.append({
            "reason": r.reason,
            "pod_name": r.pod_name,
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
            "count": r.count,
            "minutes_before": delta_min,
            "minutes_since_last": int(
                (around_aware - last_aware).total_seconds() // 60
            ),
            "message": (r.message or "")[:200],
        })
    return out


# Уровни Seq, которые трактуем как «ошибка приложения» для сигнала.
# Warning намеренно НЕ включён по умолчанию: в WO Warning — это шумный
# уровень (≈150k событий/24h vs ≈4k Error), он раздул бы rate и сделал
# сигнал бесполезным. Caller может попросить уровни явно.
_LOG_ERROR_LEVELS = ("Error", "Fatal")


def log_error_rate_for(
    db: Session,
    namespace: str,
    service_name: str,
    *,
    window_minutes: int = 60,
    levels: Optional[List[str]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Per-service лог-производный error-rate из ``kg_log_observations``.

    ВАЖНО — СЕМАНТИКА: это **log-derived proxy** уровня приложения, а НЕ
    HTTP 5xx и НЕ latency. Источник — счётчики Error/Fatal-логов из Seq
    (beat ``kg_seq_logs_sync``), агрегированные по 10-мин окнам. Сигнал
    отвечает на вопрос «сервис стал больше ругаться в логах?», но НЕ на
    «сколько запросов вернули 5xx» — лог-ошибка может быть retry'ем,
    фоновой джобой, health-probe или вообще не относиться к user-facing
    трафику. НЕ записывать это в ``kg_service_health.http_5xx_rate``:
    смешение двух разных сигналов введёт consumer'ов в заблуждение
    (http_5xx в service_health = NULL, потому что app /metrics закрыт
    JWT — WO-12483; подменять его логами было бы фальшивым «зелёным».
    Per-host/path HTTP 5xx с 2026-06-10 есть в kg_ingress_observations —
    это endpoint-разрез, а не per-service).

    Считается on-read, без новых Seq-запросов и без изменения схемы:
        rate_per_min = SUM(count за окно) / window_minutes

    Окно — [now-window_minutes, now]. ``now`` инъектится для тестов;
    по умолчанию ``datetime.utcnow()`` (ts в БД — naive UTC).

    Возвращает dict или None если сервис не в графе. Если сервис есть, но
    лог-наблюдений в окне нет — вернёт нули (это валидный сигнал «тихо»,
    в отличие от «сервиса не знаем»).

        {
          "service_id": int,
          "namespace": str,
          "name": str,
          "window_minutes": int,
          "levels": ["Error", "Fatal"],
          "error_count": int,          # SUM(count) за окно
          "log_error_rate_per_min": float,  # округл. до 3 знаков
          "buckets": int,              # сколько 10-мин строк попало
          "is_proxy": True,            # маркер: НЕ настоящий HTTP 5xx
        }

    ОГРАНИЧЕНИЯ (документируем честно):
      * ``count`` снизу ограничен Seq fetch-cap (``top_messages limit=500``
        на level/instance/окно). На очень шумном realm rate под-считан.
      * Атрибуция сервиса — best-effort матч ``App``-тэга (≈96% на recon
        2026-06-05); немэтченные строки (service_id=NULL) сюда НЕ попадают.
      * Это per-service, не per-endpoint и не per-status-code сигнал.
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return None

    use_levels = list(levels) if levels else list(_LOG_ERROR_LEVELS)
    ref = ensure_naive(now or datetime.utcnow())
    since = ref - timedelta(minutes=window_minutes)

    total, buckets = (
        db.query(
            func.coalesce(func.sum(LogObservation.count), 0),
            func.count(LogObservation.id),
        )
        .filter(
            LogObservation.service_id == svc.id,
            LogObservation.level.in_(use_levels),
            LogObservation.ts >= since,
            LogObservation.ts <= ref,
        )
        .one()
    )
    error_count = int(total or 0)
    bucket_count = int(buckets or 0)
    rate = error_count / window_minutes if window_minutes > 0 else 0.0

    return {
        "service_id": int(svc.id),
        "namespace": svc.namespace,
        "name": svc.name,
        "window_minutes": window_minutes,
        "levels": use_levels,
        "error_count": error_count,
        "log_error_rate_per_min": round(rate, 3),
        "buckets": bucket_count,
        "is_proxy": True,
    }


def ingress_health_for(
    db: Session,
    namespace: str,
    service_name: str,
    *,
    window_minutes: int = 15,
    top_n: int = 3,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Per-service HTTP RED-сигнал из ``kg_ingress_observations`` (endpoint-разрез).

    ВАЖНО — СЕМАНТИКА: это **ingress-derived** сигнал (nginx-ingress controller
    метрики per host/path), а НЕ per-service app `/metrics`. Последний закрыт
    JWT (WO-12483) → ``kg_service_health.http_5xx_rate/p95`` = NULL. Этот
    источник доступен с 2026-06-10 и покрывает сервисы с Ingress'ом. Он
    меряет трафик НА ГРАНИЦЕ (ingress), не внутренние service-to-service
    вызовы — поэтому маркируется ``is_ingress_derived=True`` и НЕ пишется в
    ``kg_service_health`` (чтобы не смешивать два разных разреза).

    Для каждого endpoint (host, path) сервиса берётся ПОСЛЕДНЯЯ запись в окне
    [now-window_minutes, now]; агрегаты по сервису — пиковые (max) 5xx/p95/p99
    и суммарный rps. top_endpoints отсортированы по 5xx desc, затем p95 desc.

    Возвращает dict (всегда, даже с нулями — endpoints_total=0 если наблюдений
    нет) либо ``{}`` если сервиса нет в графе. ``now`` инъектится для тестов.

        {
          "service_id": int, "namespace": str, "name": str,
          "window_minutes": int, "endpoints_total": int,
          "max_5xx_rate": float, "max_p95_ms": float, "max_p99_ms": float,
          "total_rps": float,
          "top_endpoints": [{host, path, error_5xx_rate, p95_latency_ms, rps}],
          "is_ingress_derived": True,
        }
    """
    svc = _service_by_namespace_name(db, namespace, service_name)
    if svc is None:
        return {}

    now = ensure_naive(now) if now is not None else datetime.utcnow()
    cutoff = now - timedelta(minutes=window_minutes)
    rows = (
        db.query(IngressObservation)
        .filter(
            IngressObservation.service_id == svc.id,
            IngressObservation.ts >= cutoff,
        )
        .order_by(IngressObservation.ts.desc())
        .all()
    )

    # Последняя запись на каждый endpoint (host, path) — rows уже ts DESC.
    latest_by_endpoint: Dict[tuple, IngressObservation] = {}
    for r in rows:
        key = (r.host, r.path)
        if key not in latest_by_endpoint:
            latest_by_endpoint[key] = r

    endpoints = list(latest_by_endpoint.values())
    result: Dict[str, Any] = {
        "service_id": svc.id,
        "namespace": namespace,
        "name": service_name,
        "window_minutes": window_minutes,
        "endpoints_total": len(endpoints),
        "max_5xx_rate": 0.0,
        "max_p95_ms": 0.0,
        "max_p99_ms": 0.0,
        "total_rps": 0.0,
        "top_endpoints": [],
        "is_ingress_derived": True,
    }
    if not endpoints:
        return result

    def _f(v: Optional[float]) -> float:
        return float(v) if v is not None else 0.0

    result["max_5xx_rate"] = round(max(_f(e.error_5xx_rate) for e in endpoints), 4)
    result["max_p95_ms"] = round(max(_f(e.p95_latency_ms) for e in endpoints), 1)
    result["max_p99_ms"] = round(max(_f(e.p99_latency_ms) for e in endpoints), 1)
    result["total_rps"] = round(sum((_f(e.rps) for e in endpoints), 0.0), 3)

    ranked = sorted(
        endpoints,
        key=lambda e: (_f(e.error_5xx_rate), _f(e.p95_latency_ms)),
        reverse=True,
    )
    result["top_endpoints"] = [
        {
            "host": e.host,
            "path": e.path,
            "error_5xx_rate": round(_f(e.error_5xx_rate), 4),
            "p95_latency_ms": round(_f(e.p95_latency_ms), 1),
            "rps": round(_f(e.rps), 3),
        }
        for e in ranked[:top_n]
    ]
    return result
