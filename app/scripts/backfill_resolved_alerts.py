"""Бэкфилл `resolved_at` для застрявших kg_alerts (one-shot CLI).

Контекст (2026-05-25): после регрессии 2026-04-10 beat task
`kg_alerts_resolve_sync` молча пропускал обновление `resolved_at`:
при exception на AM-fetch ранний return блокировал и age-fallback тоже,
поэтому stuck-firing alerts копились вечно (16 шт. с fired_at 22 мая
зарегистрированы при ручной аудите 25 мая, плюс этцд-алерты от 10 апреля).

Регрессия исправлена (см. `app/knowledge_graph/alerts_resolve_sync.py`),
но историю надо подчистить руками — beat-task новых alerts обработает
естественно, а вот legacy-stuck требует одного backfill-прогона.

Логика:
1. GET AlertManager `/api/v2/alerts` → set активных fingerprints (полный
   набор state: active/silenced/inhibited/unprocessed).
2. SELECT kg_alerts WHERE resolved_at IS NULL.
3. Для каждой записи: если fingerprint НЕ в live set → resolved_at=now.
   Записи без fingerprint (NULL/пустая) — резолвим всегда (нет способа
   проверить firing, безопаснее закрыть; age-фильтр через --min-age-hours
   как safety).
4. Маркер `raw.resolved_by='backfill_2026_05_25'` для отчётности.

Использование:

    # dry-run по умолчанию
    python -m app.scripts.backfill_resolved_alerts

    # реально применить (требует подтверждения окружения)
    python -m app.scripts.backfill_resolved_alerts --apply

    # ограничить возрастом — не трогать alerts моложе 6 часов
    python -m app.scripts.backfill_resolved_alerts --apply --min-age-hours 6

    # отключить AM-проверку (резолвим ВСЁ что > min-age-hours)
    python -m app.scripts.backfill_resolved_alerts --apply --no-am

Идемпотентность: повторный запуск ничего не делает — отфильтровано
условием `resolved_at IS NULL`.

Безопасность: write-операция в БД. user-driven, в beat task НЕ
встраивается. По умолчанию dry-run, требует явный `--apply`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, cast

from sqlalchemy.orm import Session

from app.knowledge_graph.alerts_resolve_sync import _fetch_active_fingerprints
from app.knowledge_graph.schema import AlertEvent

log = logging.getLogger(__name__)

BACKFILL_MARKER = "backfill_2026_05_25"


@dataclass
class BackfillResolvedResult:
    """Структурированный итог. Используется и в CLI и в тестах."""
    open_total: int = 0
    skipped_too_young: int = 0
    skipped_still_firing: int = 0
    would_resolve: int = 0
    actually_resolved: int = 0
    am_active_count: int = -1  # -1 = AM не использовался / недоступен
    am_error: Optional[str] = None
    sample_resolved: List[dict] = field(default_factory=list)


def _fetch_active_safe() -> tuple[Optional[Set[str]], Optional[str]]:
    """Обёртка над _fetch_active_fingerprints с пойманным exception.

    Возвращает (set | None, error_str | None). Если AM недоступен —
    set=None, error содержит сообщение.
    """
    try:
        active = asyncio.run(_fetch_active_fingerprints())
        return active, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def plan_backfill(
    db: Session,
    *,
    active_fingerprints: Optional[Set[str]],
    min_age_hours: float = 1.0,
    limit: Optional[int] = None,
) -> tuple[List[AlertEvent], BackfillResolvedResult]:
    """Собрать список AlertEvent для UPDATE. Не пишет в БД."""
    result = BackfillResolvedResult(
        am_active_count=(
            len(active_fingerprints) if active_fingerprints is not None else -1
        ),
    )
    age_cutoff = datetime.utcnow() - timedelta(hours=min_age_hours)

    open_alerts = (
        db.query(AlertEvent)
        .filter(AlertEvent.resolved_at.is_(None))
        .order_by(AlertEvent.fired_at.asc())
        .all()
    )
    result.open_total = len(open_alerts)

    to_resolve: List[AlertEvent] = []
    for ev in open_alerts:
        if ev.fired_at and ev.fired_at >= age_cutoff:
            result.skipped_too_young += 1
            continue
        if (
            active_fingerprints is not None
            and ev.fingerprint
            and ev.fingerprint in active_fingerprints
        ):
            result.skipped_still_firing += 1
            continue
        to_resolve.append(ev)
        if limit is not None and len(to_resolve) >= limit:
            break

    result.would_resolve = len(to_resolve)
    result.sample_resolved = [
        {
            "id": ev.id,
            "alertname": ev.alertname,
            "fingerprint": ev.fingerprint,
            "fired_at": ev.fired_at.isoformat() if ev.fired_at else None,
        }
        for ev in to_resolve[:5]
    ]
    return to_resolve, result


def apply_backfill(
    db: Session, plan: List[AlertEvent], marker: str = BACKFILL_MARKER
) -> int:
    """UPDATE resolved_at + raw.resolved_by для записей из плана.

    Commit-ит транзакцию. При ошибке — rollback и re-raise.
    """
    now = datetime.utcnow()
    applied = 0
    try:
        for ev in plan:
            ev.resolved_at = cast(Any, now)
            raw_existing: Dict[str, Any] = ev.raw if isinstance(ev.raw, dict) else {}
            raw = dict(raw_existing)
            raw["resolved_by"] = marker
            ev.raw = cast(Any, raw)
            applied += 1
        if applied:
            db.commit()
    except Exception:
        db.rollback()
        raise
    return applied


def run_backfill_resolved(
    db: Session,
    *,
    apply: bool = False,
    min_age_hours: float = 1.0,
    limit: Optional[int] = None,
    use_am: bool = True,
) -> BackfillResolvedResult:
    """Главная entry-point. Используется CLI и тестами."""
    active: Optional[Set[str]] = None
    am_error: Optional[str] = None
    if use_am:
        active, am_error = _fetch_active_safe()

    plan, result = plan_backfill(
        db,
        active_fingerprints=active,
        min_age_hours=min_age_hours,
        limit=limit,
    )
    result.am_error = am_error

    if apply and plan:
        result.actually_resolved = apply_backfill(db, plan)
    return result


# ── Markdown rendering ──────────────────────────────────────────────────


def render_stats(result: BackfillResolvedResult) -> str:
    am = (
        f"{result.am_active_count} active fingerprints"
        if result.am_active_count >= 0
        else "AM недоступен / отключён"
    )
    err = f" (error: {result.am_error})" if result.am_error else ""
    lines = [
        "| metric | value |",
        "|---|---|",
        f"| am_status | {am}{err} |",
        f"| open_total | {result.open_total} |",
        f"| skipped_too_young | {result.skipped_too_young} |",
        f"| skipped_still_firing | {result.skipped_still_firing} |",
        f"| would_resolve | {result.would_resolve} |",
        f"| actually_resolved | {result.actually_resolved} |",
    ]
    return "\n".join(lines)


def render_sample(result: BackfillResolvedResult) -> str:
    if not result.sample_resolved:
        return "_нет кандидатов._"
    lines = [
        "| id | alertname | fingerprint | fired_at |",
        "|---|---|---|---|",
    ]
    for s in result.sample_resolved:
        fp = (s.get("fingerprint") or "")[:16] + (
            "…" if s.get("fingerprint") and len(s["fingerprint"]) > 16 else ""
        )
        lines.append(
            f"| {s.get('id')} | {s.get('alertname')} | {fp} "
            f"| {s.get('fired_at')} |"
        )
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_resolved_alerts",
        description=(
            "Backfill resolved_at для застрявших kg_alerts. "
            "Контекст — регрессия 2026-04-10..2026-05-25."
        ),
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
        "--min-age-hours",
        type=float,
        default=1.0,
        help=(
            "не трогать alerts моложе N часов (safety от резолва свежих, "
            "default 1.0)"
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ограничить число UPDATE-ов за прогон",
    )
    p.add_argument(
        "--no-am",
        dest="use_am",
        action="store_false",
        default=True,
        help=(
            "не дергать AM API — резолвить ВСЁ что старше min-age-hours. "
            "Полезно когда AM недоступен из CLI-окружения."
        ),
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _build_argparser().parse_args(argv)
    apply = bool(args.apply) and not args.dry_run

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # Сначала dry-run проход для preview (даже если --apply, мы
        # сначала собираем план и печатаем его, потом применяем).
        active: Optional[Set[str]] = None
        am_error: Optional[str] = None
        if args.use_am:
            active, am_error = _fetch_active_safe()

        plan, result = plan_backfill(
            db,
            active_fingerprints=active,
            min_age_hours=args.min_age_hours,
            limit=args.limit,
        )
        result.am_error = am_error

        print("## Backfill plan")
        print(render_stats(result))
        print()
        print("## Sample (первые 5)")
        print(render_sample(result))
        print()

        if apply:
            if not plan:
                print("Нечего применять — план пуст.")
                return 0
            applied = apply_backfill(db, plan)
            result.actually_resolved = applied
            print(f"## Applied: {applied} alerts marked resolved")
        else:
            print("(dry-run — добавь `--apply` чтобы записать)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
