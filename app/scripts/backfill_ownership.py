"""Бэкфилл ownership + stale_class для kg_services.

Контекст (2026-05-24): в проде `owner_known` стагнировал на 12.40%
(335/2702 real services). Multi-signal owner-inference из PR #85
(`app/services/ownership_suggester.suggest_owner_multi_signal`)
интегрирован только в `unowned_namespaces_section` stats_digest, но
periodic `kg_topology_sync` его не зовёт — следовательно 2367 services
без owner НИКОГДА не пересчитываются.

Этот скрипт закрывает gap: проходит по всем `kg_services` где
`team_owner IS NULL OR team_owner = ''`, прогоняет multi-signal
inference, и при `confidence >= threshold` (либо `source=manual`)
ставит team_owner + `metadata_json.owner_source`.

Параллельный бонус — `--stale` flag для backfill-а `stale_class`
для сервисов с `stale_class IS NULL`. Использует
`classify_stale_with_deploys` (тот же классификатор что в
`kg_sync._refresh_stale_class_for_namespace`).

Использование:

    # dry-run по умолчанию, threshold 0.5
    python -m app.scripts.backfill_ownership

    # реально применить
    python -m app.scripts.backfill_ownership --apply

    # high-confidence rollout (только manual + полный fusion)
    python -m app.scripts.backfill_ownership --apply --threshold 0.7

    # пошаговый rollout — limit на batch
    python -m app.scripts.backfill_ownership --apply --limit 100

    # фильтр по ns-pattern (fnmatch glob)
    python -m app.scripts.backfill_ownership --filter-ns 'squad-*'

    # бонус: backfill stale_class
    python -m app.scripts.backfill_ownership --stale --apply

Идемпотентность: повторный запуск на тех же данных не вносит изменений
(сервисы которые уже получили owner на предыдущем run-е отфильтрованы
условием `team_owner IS NULL OR team_owner = ''`).
"""
from __future__ import annotations

import argparse
import fnmatch
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.knowledge_graph.contract import OWNER_SOURCE_ALIASES
from app.knowledge_graph.schema import Deployment, Service
from app.knowledge_graph.stale_classifier import classify_stale_with_deploys
from app.services.ownership_suggester import (
    OwnerSuggestion,
    suggest_owner_multi_signal,
)

log = logging.getLogger(__name__)


# ── Dataclasses для structured-результата ──────────────────────────────


@dataclass
class OwnerPlan:
    """Один кандидат на UPDATE team_owner."""
    service_id: int
    namespace: str
    name: str
    suggested_owner: str
    confidence: float
    sources: List[str]
    manual: bool


@dataclass
class StalePlan:
    """Один кандидат на UPDATE stale_class."""
    service_id: int
    namespace: str
    name: str
    new_class: str


@dataclass
class BackfillResult:
    """Итоговая статистика. Используется и в тестах, и в beat-task'е."""
    kept_existing: int = 0
    would_update_owner: int = 0
    skipped_low_confidence: int = 0
    actually_updated_owner: int = 0
    would_update_stale: int = 0
    actually_updated_stale: int = 0
    total_candidates_owner: int = 0
    total_candidates_stale: int = 0


# ── Core backfill helpers ──────────────────────────────────────────────


def _canonical_owner_source(sources: List[str]) -> str:
    """Из списка коротких алиасов выбрать «winner» source для metadata_json.

    Приоритет: manual > deploy_history > labels > prefix. Маппим в
    канонический slug через `OWNER_SOURCE_ALIASES`.
    """
    priority = ["manual", "deploy_history", "labels", "prefix"]
    for p in priority:
        if p in sources:
            return OWNER_SOURCE_ALIASES.get(p, p)
    if sources:
        return OWNER_SOURCE_ALIASES.get(sources[0], sources[0])
    return "suggested"


def _matches_ns_filter(ns: str, pattern: Optional[str]) -> bool:
    """Glob-фильтр по ns. Пустой pattern — всегда True."""
    if not pattern:
        return True
    return fnmatch.fnmatchcase(ns, pattern)


def plan_ownership(
    db: Session,
    *,
    threshold: float = 0.5,
    limit: Optional[int] = None,
    filter_ns: Optional[str] = None,
) -> Tuple[List[OwnerPlan], int]:
    """Собрать план UPDATE-ов для team_owner.

    Возвращает (plans, skipped_low_confidence). Не пишет в БД.
    """
    q = db.query(Service).filter(
        (Service.team_owner.is_(None)) | (Service.team_owner == "")
    )
    services = q.all()

    plans: List[OwnerPlan] = []
    skipped = 0
    for svc in services:
        ns = str(svc.namespace)
        if not _matches_ns_filter(ns, filter_ns):
            continue
        sug: OwnerSuggestion = suggest_owner_multi_signal(ns, db=db)
        if sug.owner is None:
            continue
        # Manual override — всегда применяем (confidence=1.0).
        # Иначе — проверяем threshold.
        if not sug.manual and sug.confidence < threshold:
            skipped += 1
            continue
        plans.append(OwnerPlan(
            service_id=int(svc.id),
            namespace=ns,
            name=str(svc.name),
            suggested_owner=sug.owner,
            confidence=sug.confidence,
            sources=list(sug.sources),
            manual=sug.manual,
        ))
        if limit is not None and len(plans) >= limit:
            break
    return plans, skipped


def plan_stale(
    db: Session,
    *,
    limit: Optional[int] = None,
    filter_ns: Optional[str] = None,
) -> List[StalePlan]:
    """Собрать план UPDATE-ов для stale_class.

    Берём сервисы с `stale_class IS NULL`, считаем last_deploy_at per service
    одним JOIN-ом и прогоняем `classify_stale_with_deploys`. Не пишет в БД.
    """
    services = (
        db.query(Service)
        .filter(Service.stale_class.is_(None))
        .all()
    )
    if not services:
        return []

    svc_ids = [int(s.id) for s in services]
    # max(started_at) per service одним запросом — иначе N+1.
    rows = (
        db.query(Deployment.service_id, func.max(Deployment.started_at))
        .filter(Deployment.service_id.in_(svc_ids))
        .group_by(Deployment.service_id)
        .all()
    )
    last_by_svc: Dict[int, datetime] = {sid: ts for sid, ts in rows if ts is not None}

    plans: List[StalePlan] = []
    for svc in services:
        ns = str(svc.namespace)
        if not _matches_ns_filter(ns, filter_ns):
            continue
        sid = int(svc.id)
        new_class = classify_stale_with_deploys(
            name=str(svc.name),
            namespace=ns,
            last_deploy_at=last_by_svc.get(sid),
            team_owner=str(svc.team_owner) if svc.team_owner else None,
        )
        if str(svc.stale_class or "") == new_class:
            # уже совпадает — пропускаем (idempotency)
            continue
        plans.append(StalePlan(
            service_id=sid,
            namespace=ns,
            name=str(svc.name),
            new_class=new_class,
        ))
        if limit is not None and len(plans) >= limit:
            break
    return plans


def _apply_owner_plan(db: Session, plan: OwnerPlan) -> None:
    """Записать один UPDATE: team_owner + metadata_json.owner_source.

    JSON merge делаем Python-side: читаем metadata_json, мерджим
    в новый dict, пишем обратно. Это переносимо между PG и SQLite
    (PG-only `||` op для JSONB в тестах не работает).
    """
    svc = db.query(Service).filter(Service.id == plan.service_id).first()
    if svc is None:
        return
    svc.team_owner = plan.suggested_owner  # type: ignore[assignment]
    md_existing: Dict[str, Any] = (
        svc.metadata_json if isinstance(svc.metadata_json, dict) else {}
    )
    md_new: Dict[str, Any] = dict(md_existing)
    md_new["owner_source"] = _canonical_owner_source(plan.sources)
    md_new["owner_confidence"] = round(plan.confidence, 3)
    if plan.manual:
        md_new["owner_manual"] = True
    svc.metadata_json = md_new  # type: ignore[assignment]


def _apply_stale_plan(db: Session, plan: StalePlan) -> None:
    svc = db.query(Service).filter(Service.id == plan.service_id).first()
    if svc is None:
        return
    svc.stale_class = plan.new_class  # type: ignore[assignment]


# ── Markdown rendering ──────────────────────────────────────────────────


def render_owner_table(plans: List[OwnerPlan]) -> str:
    """Markdown-таблица для dry-run вывода."""
    if not plans:
        return "_нет кандидатов._"
    lines = [
        "| ns | service | suggested_owner | confidence | source |",
        "|---|---|---|---|---|",
    ]
    for p in plans:
        src = ",".join(p.sources) if p.sources else "-"
        lines.append(
            f"| {p.namespace} | {p.name} | {p.suggested_owner} "
            f"| {p.confidence:.2f} | {src} |"
        )
    return "\n".join(lines)


def render_stale_table(plans: List[StalePlan]) -> str:
    if not plans:
        return "_нет кандидатов._"
    lines = [
        "| ns | service | new_stale_class |",
        "|---|---|---|",
    ]
    for p in plans:
        lines.append(f"| {p.namespace} | {p.name} | {p.new_class} |")
    return "\n".join(lines)


def render_stats(result: BackfillResult) -> str:
    """Итоговая markdown-таблица со статистикой."""
    return "\n".join([
        "| metric | value |",
        "|---|---|",
        f"| kept_existing | {result.kept_existing} |",
        f"| total_candidates_owner | {result.total_candidates_owner} |",
        f"| would_update_owner | {result.would_update_owner} |",
        f"| actually_updated_owner | {result.actually_updated_owner} |",
        f"| skipped_low_confidence | {result.skipped_low_confidence} |",
        f"| total_candidates_stale | {result.total_candidates_stale} |",
        f"| would_update_stale | {result.would_update_stale} |",
        f"| actually_updated_stale | {result.actually_updated_stale} |",
    ])


# ── Top-level run_backfill (используется CLI + beat task) ──────────────


def run_backfill(
    db: Session,
    *,
    apply: bool = False,
    threshold: float = 0.5,
    limit: Optional[int] = None,
    filter_ns: Optional[str] = None,
    do_ownership: bool = True,
    do_stale: bool = False,
) -> BackfillResult:
    """Главная entry-point. Используется и CLI и beat task'ом.

    apply=False → dry-run, ничего не пишет.
    apply=True  → UPDATE-ит и commit-ит.

    Не бросает на пустой результат — просто возвращает zeros.
    """
    result = BackfillResult()

    # baseline: количество сервисов которые уже имеют owner (для информации).
    kept_q = db.query(Service).filter(
        Service.team_owner.isnot(None),
        Service.team_owner != "",
    )
    result.kept_existing = kept_q.count()

    if do_ownership:
        owner_plans, skipped = plan_ownership(
            db,
            threshold=threshold,
            limit=limit,
            filter_ns=filter_ns,
        )
        result.total_candidates_owner = len(owner_plans)
        result.would_update_owner = len(owner_plans)
        result.skipped_low_confidence = skipped

        if apply:
            for op in owner_plans:
                _apply_owner_plan(db, op)
            db.commit()
            result.actually_updated_owner = len(owner_plans)

    if do_stale:
        stale_plans = plan_stale(db, limit=limit, filter_ns=filter_ns)
        result.total_candidates_stale = len(stale_plans)
        result.would_update_stale = len(stale_plans)
        if apply:
            for sp in stale_plans:
                _apply_stale_plan(db, sp)
            db.commit()
            result.actually_updated_stale = len(stale_plans)

    return result


# ── CLI ─────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_ownership",
        description="Backfill team_owner + (optional) stale_class для kg_services.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="реально записать UPDATE; без флага — dry-run (default)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="явный dry-run (default, дублирует отсутствие --apply)",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="минимальный confidence для применения owner (default 0.5)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ограничить число UPDATE-ов за прогон (для пошагового rollout)",
    )
    p.add_argument(
        "--filter-ns",
        type=str,
        default=None,
        help="fnmatch glob по namespace (напр. 'squad-*')",
    )
    p.add_argument(
        "--ownership",
        action="store_true",
        help="backfill только team_owner (default-режим)",
    )
    p.add_argument(
        "--stale",
        action="store_true",
        help="backfill stale_class (можно комбинировать с --ownership)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="и ownership и stale (эквивалент --ownership --stale)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_argparser().parse_args(argv)

    # Default-режим: только ownership. С --stale без --ownership — только stale.
    # С --all — оба. С --stale --ownership — тоже оба.
    do_ownership = args.ownership or args.all or (not args.stale and not args.all)
    do_stale = args.stale or args.all

    apply = bool(args.apply) and not args.dry_run

    # Locally-imported чтобы не пытаться открыть DB при `--help`.
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # Сначала собираем план для preview (используется и dry-run и apply).
        owner_plans: List[OwnerPlan] = []
        stale_plans: List[StalePlan] = []
        skipped = 0
        if do_ownership:
            owner_plans, skipped = plan_ownership(
                db,
                threshold=args.threshold,
                limit=args.limit,
                filter_ns=args.filter_ns,
            )
            print("## Ownership plan")
            print(render_owner_table(owner_plans))
            print()
        if do_stale:
            stale_plans = plan_stale(db, limit=args.limit, filter_ns=args.filter_ns)
            print("## Stale plan")
            print(render_stale_table(stale_plans))
            print()

        # Считаем результаты: kept_existing — реальный count из БД;
        # would_update_* — из preview-плана; skipped_low_confidence — оттуда же.
        result = BackfillResult(
            kept_existing=db.query(Service).filter(
                Service.team_owner.isnot(None),
                Service.team_owner != "",
            ).count(),
            total_candidates_owner=len(owner_plans),
            would_update_owner=len(owner_plans),
            skipped_low_confidence=skipped,
            total_candidates_stale=len(stale_plans),
            would_update_stale=len(stale_plans),
        )

        if apply:
            for op in owner_plans:
                _apply_owner_plan(db, op)
            for sp in stale_plans:
                _apply_stale_plan(db, sp)
            if owner_plans or stale_plans:
                db.commit()
            result.actually_updated_owner = len(owner_plans)
            result.actually_updated_stale = len(stale_plans)

        print("## Stats")
        print(render_stats(result))

        if not apply:
            print("\n_dry-run: ничего не записано. Передайте --apply для реальных UPDATE-ов._")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
