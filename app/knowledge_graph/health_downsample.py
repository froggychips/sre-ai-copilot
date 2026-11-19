"""Свёртка `kg_service_health`: сырьё — неделя, дальше — часовые агрегаты.

Замер 18.11.2026 (прод, v1.0.35): `kg_service_health` — 2159 МБ из 3835 МБ
базы (56%), 6,6 млн строк: ~3800 сервисов × точка каждые десять минут ×
30 дней. Это копия данных VictoriaMetrics, и читают её на неделю:

    anomaly_detection.BASELINE_DAYS            7 дней   (по сервису)
    health_score._METRIC_BASELINE_WINDOW_DAYS  7 дней   (по сервису)
    queries.orleans_health                     24 часа
    self_health.materialization_zero_rate      24 часа
    deploy_correlator                          ±минуты вокруг деплоя, деплой
                                               не старше lookback_hours
    namespace_lifecycle (чистка инкарнаций)    по service_id, без глубины
    MCP-тул kg_service_health (внешний репо)   6 часов по умолчанию, окно
                                               задаёт вызывающий

Точки старше семи суток не читает никто — 3,97 млн строк из 6,62 млн. Они
сворачиваются в `kg_service_health_hourly` (строка на сервис и час: среднее,
максимум, число точек; те же 3,97 млн строк дают 666 тысяч часовых) и
удаляются из сырья.

**Идемпотентность — транзакцией, а не проверкой.** Окно времени
сворачивается одной транзакцией: INSERT агрегатов и DELETE сырья того же
окна либо оба видны, либо оба нет. Оборванный прогон не оставляет ни
«свёрнуто, но не удалено» (следующий прогон посчитал бы точки дважды), ни
«удалено, но не свёрнуто». ON CONFLICT нужен только на случай точки,
записанной в уже свёрнутый час задним числом: она вливается в агрегат
точно — у каждой метрики хранится число её не-NULL замеров (`<метрика>_n`),
среднее взвешивается по нему, максимум — больший, счётчики складываются.

**Граница — по целым часам.** `cutoff` округляется вниз до часа: час либо
целиком сырьё, либо целиком агрегат, и строка агрегата не дописывается
следующим прогоном.

**Пол.** `raw_days < MIN_RETENTION_DAYS` — отказ: внутри семи дней лежит
baseline детектора аномалий, а свёрнутое сырьё не восстановить.

Рядом есть 30-дневный ретеншен сырья (`health_retention.RETENTION_TARGETS`).
Он действует, пока свёртка выключена или в dry_run. В режиме apply сырьё
удаляет только свёртка (`health_retention.raw_health_owner`): срочная чистка
не знает, свёрнута ли точка, и при `raw_days` больше её срока или при
отставшей свёртке удаляла бы несвёрнутое.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app.knowledge_graph.health_retention import (MIN_RETENTION_DAYS,
                                                  RETENTION_ROWS)
from app.knowledge_graph.schema import HEALTH_HOURLY_METRICS

logger = logging.getLogger(__name__)

__all__ = [
    "downsample_service_health", "purge_hourly",
    "DEFAULT_RAW_DAYS", "DEFAULT_HOURLY_DAYS",
]

#: Сколько суток живут сырые точки. Ровно baseline детектора аномалий.
DEFAULT_RAW_DAYS = 7
#: Сколько суток живут часовые агрегаты.
DEFAULT_HOURLY_DAYS = 90

#: Часов сырья на одну транзакцию. 6 часов ≈ 130 тысяч строк на проде —
#: тот же порядок, что батч ретеншена (50 тысяч), и секунды на транзакцию.
DEFAULT_CHUNK_HOURS = 6
#: Транзакций за прогон: 40 × 6 ч = десять суток сырья. Первое включение
#: разбирает накопленные 23 дня за три ночи, дальше — четыре окна в сутки.
DEFAULT_MAX_CHUNKS = 40
#: Потолок времени прогона. Глобальный soft-лимит задач — 1500 с; прогон
#: обязан выйти сам и вернуть статистику, а не быть оборванным.
DEFAULT_TIME_BUDGET_SECONDS = 900.0

_RAW = "kg_service_health"
_HOURLY = "kg_service_health_hourly"


def _floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _as_datetime(value: Any) -> Optional[datetime]:
    """min(ts) из сырого SQL: PostgreSQL отдаёт datetime, SQLite — строку."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _hour_expr(db: Session) -> str:
    """Выражение «начало часа точки» для текущего диалекта.

    У SQLite нет date_trunc; формат строки — тот же, которым SQLAlchemy
    пишет DateTime в SQLite, иначе один и тот же час из двух прогонов дал бы
    два разных ключа.
    """
    if db.get_bind().dialect.name == "postgresql":
        return "date_trunc('hour', ts)"
    return "strftime('%Y-%m-%d %H:00:00.000000', ts)"


def _fold_sql(hour_expr: str) -> str:
    """INSERT … SELECT … ON CONFLICT: свернуть окно сырья в часовые строки.

    Имена таблиц, колонок и выражение часа — литералы этого модуля и
    `HEALTH_HOURLY_METRICS`; снаружи в текст запроса ничего не попадает.
    """
    insert_cols = ["service_id", "hour", "samples"]
    select_cols = ["service_id", f"{hour_expr} AS hour", "count(*) AS samples"]
    updates = [f"samples = {_HOURLY}.samples + excluded.samples"]
    for m in HEALTH_HOURLY_METRICS:
        avg, mx, n = f"{m}_avg", f"{m}_max", f"{m}_n"
        insert_cols += [avg, mx, n]
        # count(m) считает только не-NULL — ровно те точки, по которым
        # берётся avg(m). NULLIF: «замеров не было» хранится как NULL во всех
        # трёх колонках, а не как 0 рядом с NULL-средним.
        select_cols += [
            f"avg({m}) AS {avg}", f"max({m}) AS {mx}", f"NULLIF(count({m}), 0) AS {n}",
        ]
        # Слияние с уже существующей строкой часа (поздняя точка). Среднее
        # взвешивается по числу не-NULL замеров САМОЙ метрики, не по samples:
        # точка, где метрика NULL, в samples входит, а в avg — нет (одно
        # значение 100 среди двух строк + поздняя 200 — это 150, не 133,3).
        # CASE, а не GREATEST/COALESCE-арифметика: одинаково работает в
        # PostgreSQL и SQLite, и NULL не превращается в ноль. Правые части SET
        # в обеих СУБД видят значения строки ДО обновления.
        updates.append(
            f"{avg} = CASE"
            f" WHEN excluded.{n} IS NULL THEN {_HOURLY}.{avg}"
            f" WHEN {_HOURLY}.{n} IS NULL THEN excluded.{avg}"
            f" ELSE ({_HOURLY}.{avg} * {_HOURLY}.{n}"
            f" + excluded.{avg} * excluded.{n})"
            f" / ({_HOURLY}.{n} + excluded.{n}) END"
        )
        updates.append(
            f"{mx} = CASE"
            f" WHEN excluded.{mx} IS NULL THEN {_HOURLY}.{mx}"
            f" WHEN {_HOURLY}.{mx} IS NULL THEN excluded.{mx}"
            f" WHEN excluded.{mx} > {_HOURLY}.{mx} THEN excluded.{mx}"
            f" ELSE {_HOURLY}.{mx} END"
        )
        updates.append(
            f"{n} = CASE"
            f" WHEN excluded.{n} IS NULL THEN {_HOURLY}.{n}"
            f" WHEN {_HOURLY}.{n} IS NULL THEN excluded.{n}"
            f" ELSE {_HOURLY}.{n} + excluded.{n} END"
        )
    return (
        f"INSERT INTO {_HOURLY} ({', '.join(insert_cols)}) "  # nosec B608
        f"SELECT {', '.join(select_cols)} FROM {_RAW} "
        f"WHERE ts >= :lo AND ts < :hi "
        f"GROUP BY service_id, {hour_expr} "
        f"ON CONFLICT (service_id, hour) DO UPDATE SET {', '.join(updates)}"
    )


def _oldest_raw(db: Session, lo: Optional[datetime], cutoff: datetime) -> Optional[datetime]:
    """Самая старая сырая точка в [lo, cutoff). Один проход по индексу ts."""
    if lo is None:
        row = db.execute(
            text(f"SELECT min(ts) FROM {_RAW} WHERE ts < :cutoff"),  # nosec B608
            {"cutoff": cutoff},
        ).scalar()
    else:
        row = db.execute(
            text(f"SELECT min(ts) FROM {_RAW} WHERE ts >= :lo AND ts < :cutoff"),  # nosec B608
            {"lo": lo, "cutoff": cutoff},
        ).scalar()
    return _as_datetime(row)


def downsample_service_health(
    db: Session,
    *,
    raw_days: int = DEFAULT_RAW_DAYS,
    dry_run: bool = True,
    chunk_hours: int = DEFAULT_CHUNK_HOURS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Свернуть сырые точки старше `raw_days` в часовые агрегаты.

    `dry_run=True` (дефолт) ничего не пишет: считает, сколько сырых строк
    ушло бы (`would_fold`) и сколько часовых получилось бы
    (`would_create_hourly`).

    Возвращает статистику прогона:
      * `folded` — сырых строк свёрнуто и удалено;
      * `hourly_upserted` — строк агрегатов записано;
      * `chunks` — транзакций выполнено;
      * `truncated` — упёрлись в `max_chunks` или бюджет времени, остаток
        уйдёт следующим прогоном (самое старое — первым);
      * `cutoff` — граница (целый час), старше которой сворачивали;
      * `skipped` — причина отказа, если ничего не делали.
    """
    stats: Dict[str, Any] = {
        "dry_run": dry_run, "folded": 0, "hourly_upserted": 0, "chunks": 0,
        "truncated": False, "cutoff": None, "skipped": None,
    }
    if raw_days < MIN_RETENTION_DAYS:
        logger.error(
            "health_downsample.refused raw_days=%s < min=%s", raw_days, MIN_RETENTION_DAYS,
        )
        stats["skipped"] = (
            f"raw_days={raw_days} меньше минимума {MIN_RETENTION_DAYS}: внутри "
            "этого окна сырьё читает baseline детектора аномалий"
        )
        return stats
    if chunk_hours < 1 or max_chunks < 1:
        stats["skipped"] = "chunk_hours и max_chunks должны быть ≥ 1"
        return stats

    cutoff = _floor_hour((now or datetime.utcnow()) - timedelta(days=raw_days))
    stats["cutoff"] = cutoff.isoformat()
    hour_expr = _hour_expr(db)

    if dry_run:
        would_fold = int(db.execute(
            text(f"SELECT count(*) FROM {_RAW} WHERE ts < :cutoff"),  # nosec B608
            {"cutoff": cutoff},
        ).scalar() or 0)
        would_hourly = int(db.execute(
            text(
                f"SELECT count(*) FROM (SELECT 1 FROM {_RAW} "  # nosec B608
                f"WHERE ts < :cutoff GROUP BY service_id, {hour_expr}) AS g"
            ),
            {"cutoff": cutoff},
        ).scalar() or 0)
        db.rollback()   # чтение не должно оставлять открытую транзакцию
        stats["would_fold"] = would_fold
        stats["would_create_hourly"] = would_hourly
        RETENTION_ROWS.labels(target=_RAW, action="would_fold").inc(would_fold)
        RETENTION_ROWS.labels(target=_HOURLY, action="would_upsert").inc(would_hourly)
        logger.info("health_downsample.dry_run %s", stats)
        return stats

    fold_sql = text(_fold_sql(hour_expr))
    delete_sql = text(f"DELETE FROM {_RAW} WHERE ts >= :lo AND ts < :hi")  # nosec B608
    started = time.monotonic()
    oldest = _oldest_raw(db, None, cutoff)

    while oldest is not None:
        if stats["chunks"] >= max_chunks or time.monotonic() - started > time_budget_seconds:
            stats["truncated"] = True
            break
        lo = _floor_hour(oldest)
        hi = min(lo + timedelta(hours=chunk_hours), cutoff)
        try:
            upserted = cast(CursorResult, db.execute(fold_sql, {"lo": lo, "hi": hi})).rowcount or 0
            removed = cast(CursorResult, db.execute(delete_sql, {"lo": lo, "hi": hi})).rowcount or 0
            # Один commit на окно: агрегаты и удаление сырья видны только вместе.
            db.commit()
        except Exception:
            db.rollback()
            raise
        stats["chunks"] += 1
        stats["folded"] += removed
        stats["hourly_upserted"] += upserted
        # Следующее окно — от ближайшей оставшейся точки, а не lo + шаг:
        # дыра в данных (сутки простоя синка) не съедает лимит окон впустую.
        oldest = _oldest_raw(db, hi, cutoff)

    db.rollback()   # закрыть транзакцию последнего чтения
    RETENTION_ROWS.labels(target=_RAW, action="folded").inc(stats["folded"])
    RETENTION_ROWS.labels(target=_HOURLY, action="hourly_upserted").inc(stats["hourly_upserted"])
    if stats["truncated"]:
        logger.warning(
            "health_downsample.truncated folded=%s chunks=%s — остаток уйдёт "
            "следующим прогоном", stats["folded"], stats["chunks"],
        )
    logger.info("health_downsample.done %s", stats)
    return stats


def purge_hourly(
    db: Session,
    *,
    retention_days: int = DEFAULT_HOURLY_DAYS,
    raw_days: int = DEFAULT_RAW_DAYS,
    dry_run: bool = True,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Удалить часовые агрегаты старше `retention_days`.

    Удаление — окнами по суткам (`hour` в [lo, lo + 1 день)), коммит на
    окно: у таблицы составной ключ без суррогатного id, а по времени её
    режет BRIN-индекс. Срок не может быть короче срока сырья — иначе
    агрегат удалялся бы раньше, чем появлялся.
    """
    stats: Dict[str, Any] = {
        "dry_run": dry_run, "deleted": 0, "chunks": 0, "truncated": False,
        "cutoff": None, "skipped": None,
    }
    floor = max(MIN_RETENTION_DAYS, raw_days)
    if retention_days < floor:
        logger.error(
            "health_downsample.hourly_refused retention_days=%s < min=%s",
            retention_days, floor,
        )
        stats["skipped"] = (
            f"retention_days={retention_days} меньше минимума {floor} "
            "(срок хранения сырья)"
        )
        return stats

    cutoff = _floor_hour((now or datetime.utcnow()) - timedelta(days=retention_days))
    stats["cutoff"] = cutoff.isoformat()

    if dry_run:
        would = int(db.execute(
            text(f"SELECT count(*) FROM {_HOURLY} WHERE hour < :cutoff"),  # nosec B608
            {"cutoff": cutoff},
        ).scalar() or 0)
        db.rollback()
        stats["would_delete"] = would
        RETENTION_ROWS.labels(target=_HOURLY, action="would_delete").inc(would)
        logger.info("health_downsample.hourly_dry_run %s", stats)
        return stats

    min_sql = text(f"SELECT min(hour) FROM {_HOURLY} WHERE hour < :cutoff")  # nosec B608
    delete_sql = text(f"DELETE FROM {_HOURLY} WHERE hour >= :lo AND hour < :hi")  # nosec B608
    while True:
        oldest = _as_datetime(db.execute(min_sql, {"cutoff": cutoff}).scalar())
        if oldest is None:
            break
        if stats["chunks"] >= max_chunks:
            stats["truncated"] = True
            break
        lo = _floor_hour(oldest)
        hi = min(lo + timedelta(days=1), cutoff)
        removed = cast(CursorResult, db.execute(delete_sql, {"lo": lo, "hi": hi})).rowcount or 0
        db.commit()
        stats["chunks"] += 1
        stats["deleted"] += removed

    db.rollback()
    RETENTION_ROWS.labels(target=_HOURLY, action="deleted").inc(stats["deleted"])
    logger.info("health_downsample.hourly_done %s", stats)
    return stats
