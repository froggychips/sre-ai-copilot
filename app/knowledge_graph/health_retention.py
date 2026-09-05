"""Ретеншен наблюдательных таблиц — история сэмплов не должна расти вечно.

Замер 15.08.2026: 18.2 млн строк, таблица 4.2 ГБ из 5.3 ГБ всей базы (79%).
Данные копятся с 22.05.2026, темп — около 330 тысяч точек в сутки. Политики
хранения не было вообще: таблица просто росла с первого дня.

**Сколько нужно на самом деле.** Глубина, на которую эти данные реально
читают, оказалась много меньше накопленного:

    anomaly_detection.BASELINE_DAYS            7 дней
    health_score._METRIC_BASELINE_WINDOW_DAYS  7 дней
    deploy_correlator.lookback_hours           2 часа

То есть при 85 днях истории самый глубокий потребитель смотрит на неделю.
Дефолт 30 дней даёт четырёхкратный запас к этому окну и всё равно убирает
больше половины таблицы.

**Почему не по доле.** Соблазн «удалять, пока таблица не похудеет на N%»
здесь такой же ложный, как guard `drift_pct > 20%` в namespace-уборке: доля
привязывает решение к текущему состоянию, а не к смыслу. Срок хранения —
свойство данных («метрики недельной давности не нужны никому»), и считать его
надо по времени.

**Защита от собственной ошибки.** Единственный способ навредить этим модулем —
ошибиться в cutoff и снести свежее. Поэтому `MIN_RETENTION_DAYS` — жёсткий пол:
запрос удалить историю за меньший срок не выполняется, а возвращает отказ.
Оставить лишние данные не страшно; удалить нужные — необратимо, бэкапов
per-table у этой БД нет.

Удаление идёт батчами: единственный `DELETE` на 9.8 млн строк держал бы
ACCESS EXCLUSIVE-подобную нагрузку на автовакуум и раздул бы WAL. Каждый батч
коммитится отдельно, прогон ограничен `max_batches` — задача обязана
укладываться в свой тик, а не догонять всё за раз.

**Почему теперь не одна таблица.** Политика была написана для
`kg_service_health` и на ней же и осталась, хотя рядом растут ещё пять
таблиц ровно той же природы — периодические сэмплы, которые читают на
глубину часов. Замер 05.09.2026, строк старше 30 дней:

    kg_anomaly_observations    908 148  (68% таблицы, 638 МБ)
    kg_signal_aggregates       412 167  (50%,          190 МБ)
    kg_ingress_observations    350 196  (73%,          212 МБ)
    kg_log_observations        106 141  (67%,           62 МБ)
    kg_cluster_observations     21 724  (73%,           15 МБ)

При том что самые глубокие потребители смотрят на 1 час
(`VOLUME_GUARD_WINDOW`), 24 часа (self-health, team-digest) и 7 дней
(`BASELINE_DAYS`). `kg_cluster_observations` живёт дольше остальных: она
копеечная по объёму, а поквартальный тренд по кластеру — единственное, из
чего его вообще можно восстановить.

**Чего эта политика НЕ касается.** `kg_deployments`, `kg_alerts`,
`kg_pod_events`, `kg_k8s_jobs` — это события со смыслом, а не сэмплы:
деплой полугодовой давности отвечает на вопрос «когда это сломалось», и
срок его жизни — отдельное решение, не побочный эффект уборки метрик.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = [
    "purge_old_health", "purge_observations", "RETENTION_TARGETS",
    "DEFAULT_RETENTION_DAYS", "MIN_RETENTION_DAYS",
]

#: Сколько истории храним. 30 дней = 4× самое глубокое окно потребителя (7д).
DEFAULT_RETENTION_DAYS = 30

#: Пол ретеншена. Ниже него модуль отказывается работать: 7 дней — это ровно
#: baseline детектора аномалий, и удаление внутри этого окна сломало бы
#: сравнение «сейчас против нормы», причём молча.
MIN_RETENTION_DAYS = 7

#: Строк за батч. 50k — компромисс: реже коммитов, чем при 10k, и заметно
#: короче транзакция, чем при 500k.
DEFAULT_BATCH_SIZE = 50_000

#: Батчей за прогон. 40 × 50k = до 2 млн строк за тик: первый прогон разберёт
#: накопленное за несколько суток, дальше уборка станет копеечной.
DEFAULT_MAX_BATCHES = 40


#: Наблюдательные таблицы: (таблица, колонка времени, срок хранения).
#: Срок — свойство данных, а не текущего размера таблицы: см. рассуждение
#: про доли в докстринге модуля.
RETENTION_TARGETS: tuple[tuple[str, str, int], ...] = (
    # Самый глубокий потребитель — baseline детектора аномалий (7 дней).
    ("kg_service_health", "ts", DEFAULT_RETENTION_DAYS),
    # Читают на 1 час (VOLUME_GUARD_WINDOW) и 24 часа (self-health).
    ("kg_anomaly_observations", "ts", DEFAULT_RETENTION_DAYS),
    # Читают на _SIGNAL_AGG_FRESHNESS_HOURS и окно team-digest (24 часа).
    ("kg_signal_aggregates", "window_end", DEFAULT_RETENTION_DAYS),
    # Читают на минуты (_INGRESS_RECENT_WINDOW_MINUTES).
    ("kg_ingress_observations", "ts", DEFAULT_RETENTION_DAYS),
    # Окно вокруг инцидента (часы) плюс BASELINE_DAYS для log_error_rate.
    ("kg_log_observations", "ts", DEFAULT_RETENTION_DAYS),
    # 15 МБ на три месяца: дешевле хранить, чем потом не иметь тренда.
    ("kg_cluster_observations", "ts", 90),
)


def purge_observations(
    db: Session,
    *,
    overrides: Dict[str, int] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Прогнать ретеншен по всем таблицам из `RETENTION_TARGETS`.

    `overrides` — сроки из настроек по имени таблицы (сейчас так приходит
    `KG_HEALTH_RETENTION_DAYS`). Пол `MIN_RETENTION_DAYS` действует и на них.

    Таблицы обрабатываются по очереди и независимо: отказ или ошибка на
    одной не отменяет уборку остальных — иначе одна кривая настройка
    остановила бы политику целиком, а именно так эти таблицы и росли.
    """
    per_table: Dict[str, Any] = {}
    deleted = 0
    for table, ts_column, days in RETENTION_TARGETS:
        try:
            stats = purge_table(
                db, table=table, ts_column=ts_column,
                retention_days=(overrides or {}).get(table, days),
                batch_size=batch_size, max_batches=max_batches, now=now,
            )
        except Exception as e:  # noqa: BLE001 — соседние таблицы не виноваты
            logger.error("retention.table_failed table=%s: %s", table, e)
            db.rollback()
            per_table[table] = {"error": str(e)}
            continue
        per_table[table] = stats
        deleted += stats["deleted"]
    logger.info("retention.done deleted=%s tables=%s", deleted, len(per_table))
    return {"deleted": deleted, "per_table": per_table}


def purge_old_health(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Удалить точки `kg_service_health` старше `retention_days`.

    Частный случай `purge_table` — оставлен как есть ради вызывающих,
    которые знают только про метрики.
    """
    return purge_table(
        db, table="kg_service_health", ts_column="ts",
        retention_days=retention_days, batch_size=batch_size,
        max_batches=max_batches, now=now,
    )


#: Имена таблиц и колонок подставляются в SQL текстом (параметризовать
#: идентификаторы нельзя), поэтому берутся только из `RETENTION_TARGETS` и
#: сверяются с ним же: снаружи произвольное имя таблицы сюда не попадёт.
_ALLOWED_TABLES = {t: c for t, c, _ in RETENTION_TARGETS}


def purge_table(
    db: Session,
    *,
    table: str,
    ts_column: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Удалить строки `table` старше `retention_days` по колонке `ts_column`.

    Возвращает статистику прогона:
      * `deleted` — сколько строк удалено;
      * `batches` — сколько батчей выполнено;
      * `truncated` — упёрлись в `max_batches` (осталось на следующий тик);
      * `cutoff` — граница, старше которой удаляли;
      * `skipped` — причина отказа, если ничего не делали.

    Отказ (ничего не удаляя) при `retention_days < MIN_RETENTION_DAYS`:
    такой запрос почти наверняка ошибка, а последствия необратимы.
    """
    if _ALLOWED_TABLES.get(table) != ts_column:
        raise ValueError(
            f"{table}.{ts_column} нет в RETENTION_TARGETS: срок хранения "
            "таблицы — решение, которое принимают явно, а не аргументом "
            "вызова"
        )
    if retention_days < MIN_RETENTION_DAYS:
        logger.error(
            "retention.refused table=%s retention_days=%s < min=%s",
            table, retention_days, MIN_RETENTION_DAYS,
        )
        return {
            "deleted": 0, "batches": 0, "truncated": False, "cutoff": None,
            "skipped": (
                f"retention_days={retention_days} меньше минимума "
                f"{MIN_RETENTION_DAYS}: внутри этого окна лежит baseline "
                f"детектора аномалий"
            ),
        }

    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    deleted = 0
    batches = 0

    for _ in range(max_batches):
        # DELETE ... WHERE id IN (SELECT ... LIMIT n) — портируемый способ
        # ограничить батч: у PostgreSQL нет LIMIT в DELETE, а ORDER BY ts
        # даёт предсказуемый порядок (сначала самое старое).
        # cast: у DML-запроса execute() возвращает CursorResult с rowcount,
        # но стабы объявляют общий Result — по нему счётчика не видно.
        result = cast(
            CursorResult,
            db.execute(
                # nosec B608 — имя таблицы и колонки не приходят снаружи: оба
                # сверены с RETENTION_TARGETS выше (ValueError на чужое), а
                # параметризовать идентификаторы в SQL нельзя. Значения
                # (cutoff, lim) идут bind-параметрами.
                text(
                    f"DELETE FROM {table} WHERE id IN ("  # nosec B608
                    f"  SELECT id FROM {table}"
                    f"  WHERE {ts_column} < :cutoff"
                    f"  ORDER BY {ts_column} LIMIT :lim"
                    ")"
                ),
                {"cutoff": cutoff, "lim": batch_size},
            ),
        )
        # Коммит на каждый батч: транзакция живёт ровно один батч, и
        # прерывание прогона не откатывает уже сделанную работу.
        db.commit()

        rows = result.rowcount or 0
        deleted += rows
        batches += 1
        if rows < batch_size:
            break   # старых строк больше не осталось

    truncated = batches >= max_batches and deleted >= max_batches * batch_size
    stats: Dict[str, Any] = {
        "table": table,
        "deleted": deleted,
        "batches": batches,
        "truncated": truncated,
        "cutoff": cutoff.isoformat(),
        "skipped": None,
    }
    if truncated:
        # Молча оставленный хвост выглядел бы как «убрано всё».
        logger.warning(
            "retention.truncated table=%s deleted=%s — упёрлись в "
            "max_batches=%s, остаток уйдёт следующим тиком",
            table, deleted, max_batches,
        )
    logger.info("retention.table_done %s", stats)
    return stats
