"""Ретеншен `kg_service_health` — история метрик не должна расти вечно.

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
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["purge_old_health", "DEFAULT_RETENTION_DAYS", "MIN_RETENTION_DAYS"]

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


def purge_old_health(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Удалить точки `kg_service_health` старше `retention_days`.

    Возвращает статистику прогона:
      * `deleted` — сколько строк удалено;
      * `batches` — сколько батчей выполнено;
      * `truncated` — упёрлись в `max_batches` (осталось на следующий тик);
      * `cutoff` — граница, старше которой удаляли;
      * `skipped` — причина отказа, если ничего не делали.

    Отказ (ничего не удаляя) при `retention_days < MIN_RETENTION_DAYS`:
    такой запрос почти наверняка ошибка, а последствия необратимы.
    """
    if retention_days < MIN_RETENTION_DAYS:
        logger.error(
            "health_retention.refused retention_days=%s < min=%s",
            retention_days, MIN_RETENTION_DAYS,
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
        result = db.execute(
            text(
                "DELETE FROM kg_service_health WHERE id IN ("
                "  SELECT id FROM kg_service_health"
                "  WHERE ts < :cutoff ORDER BY ts LIMIT :lim"
                ")"
            ),
            {"cutoff": cutoff, "lim": batch_size},
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
        "deleted": deleted,
        "batches": batches,
        "truncated": truncated,
        "cutoff": cutoff.isoformat(),
        "skipped": None,
    }
    if truncated:
        # Молча оставленный хвост выглядел бы как «убрано всё».
        logger.warning(
            "health_retention.truncated deleted=%s — упёрлись в max_batches=%s, "
            "остаток уйдёт следующим тиком", deleted, max_batches,
        )
    logger.info("health_retention.done %s", stats)
    return stats
