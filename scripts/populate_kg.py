#!/usr/bin/env python3
"""Batch backfill knowledge graph из внешних источников.

Этот скрипт — точка входа для одноразового / периодического backfill-а
KG данными, которые pipeline сам не видит:

  * k8s API: реальный список Services / Deployments в namespace-ах
  * TeamCity history: deploy-события за период
  * Alertmanager dump: исторические alerts

Сейчас содержит:
  * working backfill из IncidentRecord-ов в БД (запускает auto_populator
    на каждом сохранённом инциденте — полезно после первого деплоя
    fact-anchored архитектуры на существующую БД).
  * Stub-ы k8s/TC/alertmanager backfill с TODO-маркерами.

Usage:
    python scripts/populate_kg.py --from-incidents     # из IncidentRecord
    python scripts/populate_kg.py --from-k8s NS        # из k8s API
    python scripts/populate_kg.py --from-teamcity DAYS # из TC history
"""
from __future__ import annotations

import argparse
import sys
from typing import List

import structlog

from app.database import IncidentRecord, SessionLocal
from app.knowledge_graph.auto_populator import populate_from_incident
from app.models.incident import Incident

logger = structlog.get_logger()


def backfill_from_incidents(limit: int = 0) -> dict:
    """Прогон auto_populator по всем IncidentRecord в БД.

    Идемпотентно (upsert_service / record_alert_event by fingerprint),
    повторный прогон не дублирует. limit=0 — без ограничения.
    """
    db = SessionLocal()
    totals = {"records": 0, "services_touched": 0,
              "deploys_added": 0, "alerts_added": 0, "errors": 0}
    try:
        q = db.query(IncidentRecord)
        if limit:
            q = q.limit(limit)
        for rec in q.all():
            totals["records"] += 1
            try:
                incident = Incident(**(rec.data or {}))
            except Exception as e:
                logger.warning("backfill.skip_invalid_record",
                               incident_id=rec.incident_id,
                               error=type(e).__name__)
                totals["errors"] += 1
                continue
            stats = populate_from_incident(db, incident)
            for k in ("services_touched", "deploys_added", "alerts_added"):
                totals[k] += stats[k]
        db.commit()
    finally:
        db.close()
    return totals


def backfill_from_k8s(namespaces: List[str]) -> dict:
    """TODO: вычитать Services / Deployments из k8s API.

    План:
      1. load_incluster_config() / load_kube_config()
      2. CoreV1Api.list_namespaced_service для каждого ns
      3. AppsV1Api.list_namespaced_deployment для каждого ns
      4. Edges: парсить selectors → matched pod labels → derived calls
         (это эвристика, точный mesh требует Istio/Linkerd API).
    """
    raise NotImplementedError(
        "k8s backfill — реализовать после первого боевого инцидента "
        "(когда станет понятно, какие labels реально несут service identity)."
    )


def backfill_from_teamcity(lookback_days: int) -> dict:
    """TODO: вычитать deployment-history через TeamCity REST.

    План:
      1. TeamCity REST `/app/rest/builds?locator=branch:(default:any)`
      2. Маппинг buildtype_id → service_name (требует config: какие
         build-конфигурации деплоят какой сервис).
      3. record_deployment() для каждого SUCCESS-build-а.
    """
    raise NotImplementedError(
        "TeamCity backfill — нужен mapping buildtype_id → service. "
        "Сейчас этот mapping живёт в config.TEAMCITY_BUILDTYPE_TO_SERVICE "
        "(не существует, см. issue/TODO)."
    )


def backfill_from_alertmanager(dump_path: str) -> dict:
    """TODO: импорт alert dump-а (JSON export из alertmanager UI или API).

    План:
      1. Парсить JSON dump
      2. record_alert_event() для каждого (идемпотентно по fingerprint).
    """
    raise NotImplementedError(
        "alertmanager dump backfill — нужен формат dump-а (зависит "
        "от того, через что выгружать: UI export / API / persistent volume)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--from-incidents", action="store_true",
        help="backfill graph из существующих IncidentRecord-ов в БД",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="ограничить число инцидентов (для --from-incidents); 0 = без лимита",
    )
    parser.add_argument(
        "--from-k8s", nargs="+", metavar="NS",
        help="(TODO) backfill из k8s API для заданных namespace",
    )
    parser.add_argument(
        "--from-teamcity", type=int, metavar="DAYS",
        help="(TODO) backfill deploys из TC за последние N дней",
    )
    parser.add_argument(
        "--from-alertmanager-dump", metavar="PATH",
        help="(TODO) backfill alerts из JSON dump alertmanager",
    )
    args = parser.parse_args()

    if not any([
        args.from_incidents, args.from_k8s,
        args.from_teamcity, args.from_alertmanager_dump,
    ]):
        parser.error("укажи источник: --from-incidents / --from-k8s / ...")

    if args.from_incidents:
        totals = backfill_from_incidents(limit=args.limit)
        print(f"backfill from incidents: {totals}")

    if args.from_k8s:
        try:
            print(backfill_from_k8s(args.from_k8s))
        except NotImplementedError as e:
            print(f"SKIP from-k8s: {e}", file=sys.stderr)

    if args.from_teamcity:
        try:
            print(backfill_from_teamcity(args.from_teamcity))
        except NotImplementedError as e:
            print(f"SKIP from-teamcity: {e}", file=sys.stderr)

    if args.from_alertmanager_dump:
        try:
            print(backfill_from_alertmanager(args.from_alertmanager_dump))
        except NotImplementedError as e:
            print(f"SKIP from-alertmanager: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
