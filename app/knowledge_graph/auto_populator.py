"""Авто-наполнение knowledge graph из событий pipeline.

Каждый инцидент проходит через `async_process_incident` — это единственная
точка, где мы гарантированно видим Service + Alert + (опционально)
Deployments из TeamCity context. Используем её для инкрементального
заполнения графа БЕЗ отдельной cron-задачи.

Что НЕ делается здесь (см. backfill_cli.py / отдельный populator):
  * Service-edges (calls/reads_from/...) — требует service-mesh или
    статического анализа, отдельная задача.
  * Backfill за прошлый период — нужен dump alertmanager / TC API.
  * Удаление сервисов которые ушли из k8s.

Конструктивно: tolerant к ошибкам. Граф — best-effort enrichment,
pipeline не должен падать, если populator упал.
"""
from __future__ import annotations

from app.core.timeutil import parse_ts
from typing import Dict, cast

import structlog
from sqlalchemy.orm import Session

from app.knowledge_graph.populator import (record_alert_event,
                                           record_deployment, upsert_service)
from app.models.incident import Incident

logger = structlog.get_logger()



def populate_from_incident(db: Session, incident: Incident) -> Dict[str, int]:
    """Записать узлы графа из одного инцидента.

    Returns:
        dict с количествами: services_touched, deploys_added, alerts_added.
        Используется для логирования и audit.
    """
    stats = {"services_touched": 0, "deploys_added": 0, "alerts_added": 0}

    labels = incident.labels or {}
    # STORE-путь синхронизирован с enrichment-путём: у kube-resource-алертов
    # (KubeDeployment*/StatefulSet*/DaemonSet*) лейбл `service` = метрика-источник
    # kube-state-metrics (`vm-kube-state-metrics`), не target. resolve_store_service
    # берёт target workload при наличии deployment/statefulset/daemonset, иначе —
    # прежнюю fallback-цепочку `service→app→deployment` (app-алерты не трогаем).
    from app.services.alert_enrichment import resolve_store_service
    service_name = resolve_store_service(
        labels,
        legacy_default=(
            labels.get("service") or labels.get("app") or labels.get("deployment")
        ),
    )
    namespace = incident.namespace
    if not (namespace and service_name):
        # Без identifiable service в графе хранить нечего.
        logger.debug(
            "kg.populate.skipped_no_service",
            incident_id=incident.incident_id,
            namespace=namespace,
        )
        return stats

    try:
        team = labels.get("team") or labels.get("squad")
        with db.begin_nested():
            svc = upsert_service(
                db, namespace=namespace, name=service_name,
                team_owner=team,
                metadata={k: v for k, v in labels.items() if k in {"app", "component", "version"}},
            )
        stats["services_touched"] += 1
    except Exception as e:
        # begin_nested() уже откатил SAVEPOINT при выходе по исключению —
        # Session очищена от aborted-состояния, внешняя транзакция цела.
        # Тут можно выйти: без service остальные записи бессмысленны.
        logger.warning(
            "kg.populate.service_failed",
            error=type(e).__name__, message=str(e),
        )
        return stats

    # Deployments из TeamCity context (если был enrichment).
    tc = incident.teamcity_context or {}
    for b in tc.get("recent_builds") or []:
        started_at = parse_ts(b.get("started_at") or b.get("finished_at"))
        if started_at is None:
            continue
        # SHA живёт в changes[0]["version"] — TC context не имеет поля "sha" напрямую
        changes = b.get("changes") or []
        sha = (changes[0].get("version") or None) if changes else None
        # KG H1: _parse_ts может вернуть None даже на непустой строке (мусорный
        # finished_at у running-билдов) — тогда `.replace()` бросал AttributeError
        # и весь build молча терялся (ловился внешним except). Считаем через
        # промежуточную переменную и .replace() только если распарсилось.
        _fin = parse_ts(b.get("finished_at"))
        try:
            with db.begin_nested():
                record_deployment(
                    db,
                    service=svc,
                    # parse_ts уже вернул naive UTC (app/core/timeutil.py):
                    # обрезать tzinfo второй раз не нужно, а голый
                    # .replace() здесь был бы неверен для источника
                    # со смещением.
                    started_at=started_at,
                    finished_at=_fin.replace(tzinfo=None) if _fin else None,
                    sha=sha,
                    repo=b.get("repo"),
                    buildtype_id=b.get("buildtype_id"),
                    build_number=str(b.get("number") or ""),
                    status=b.get("status"),
                    triggered_by=b.get("triggered_by"),
                    extras={"branch": b.get("branch")} if b.get("branch") else None,
                )
            stats["deploys_added"] += 1
        except Exception as e:
            # Битый build не должен валить остальные builds/alert этого
            # инцидента. begin_nested() откатил SAVEPOINT только этого item,
            # прежде записанные узлы и внешняя транзакция остаются целы.
            logger.warning(
                "kg.populate.deployment_failed",
                error=type(e).__name__, message=str(e),
            )

    # AlertEvent — идемпотентен по fingerprint.
    fired_at = parse_ts(incident.starts_at)
    if fired_at is not None:
        try:
            with db.begin_nested():
                record_alert_event(
                    db,
                    service=svc,
                    alertname=labels.get("alertname") or "unknown",
                    severity=labels.get("severity") or incident.severity,
                    fingerprint=incident.incident_id,  # fingerprint == incident_id
                    fired_at=fired_at.replace(tzinfo=None),
                    # Временное значение: attach_alert ниже заменит его на
                    # incident_key инцидента сервиса. Оставлено, чтобы строка
                    # без инцидента (attach упал) не осталась с NULL.
                    incident_id=incident.incident_id,
                    raw={"description": incident.description},
                )
            stats["alerts_added"] += 1
        except Exception as e:
            # begin_nested() откатил SAVEPOINT этого item; Session чиста.
            logger.warning(
                "kg.populate.alert_failed",
                error=type(e).__name__, message=str(e),
            )
        else:
            # Incident как объект графа: алерт → открытый инцидент сервиса
            # (или новый). Отдельный SAVEPOINT: сбой здесь не должен отменять
            # уже записанный алерт.
            try:
                from app.knowledge_graph.incidents import attach_alert
                with db.begin_nested():
                    inc = attach_alert(
                        db,
                        namespace=namespace,
                        service_name=service_name,
                        service_id=cast(int, svc.id),
                        fired_at=fired_at.replace(tzinfo=None),
                        alertname=labels.get("alertname") or "unknown",
                        severity=labels.get("severity") or incident.severity,
                        fingerprint=incident.incident_id,
                    )
                stats["kg_incident_id"] = cast(int, inc.id)
            except Exception as e:
                logger.warning(
                    "kg.populate.incident_attach_failed",
                    error=type(e).__name__, message=str(e),
                )

    logger.info(
        "kg.populate.done",
        incident_id=incident.incident_id,
        service=service_name,
        namespace=namespace,
        **stats,
    )
    return stats
