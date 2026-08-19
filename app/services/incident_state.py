"""Состояние обработки инцидента: колонки как источник истины, JSON как хвост.

Состояния доставки и исполнения жили внутри `incidents.analysis` — девять
ключей, среди которых два координационных примитива: outbox отчёта
(`report_pending`/`report_sent`/`report_failed`) и claim исполнителя
(`executor_in_flight`). Миграция 20260819_0200 вынесла их в колонки.

Этот модуль — единственное место, где живёт правило перехода. Он нужен, пока
существуют записи, сделанные до миграции: код читает колонку, а если её ещё
не заполнили — смотрит в старые JSON-ключи. Так миграция и выкат перестают
быть связаны порядком: неважно, что применилось раньше.

Почему разделение прошло именно здесь. В колонки ушло то, **по чему ищут и
координируются**: состояние и время. В JSON осталось то, **что показывают**:
поля embed, текст ошибки, результат исполнения. Индексировать нужно первое,
а второе всё равно читается вместе со строкой.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

#: Значения `report_state`.
REPORT_PENDING = "pending"
REPORT_SENT = "sent"
REPORT_FAILED = "failed"

#: Значения `executor_state`.
EXECUTOR_IN_FLIGHT = "in_flight"
EXECUTOR_APPLIED = "applied"
EXECUTOR_STATE_UNKNOWN = "state_unknown"
EXECUTOR_DISABLED = "disabled"

#: Соответствие «старый ключ в analysis» → «значение колонки». Порядок важен:
#: терминальные состояния перекрывают pending, если в JSON остались оба.
_REPORT_LEGACY = (
    ("report_failed", REPORT_FAILED),
    ("report_sent", REPORT_SENT),
    ("report_pending", REPORT_PENDING),
)
_EXECUTOR_LEGACY = (
    ("executor_applied", EXECUTOR_APPLIED),
    ("executor_state_unknown", EXECUTOR_STATE_UNKNOWN),
    ("executor_in_flight", EXECUTOR_IN_FLIGHT),
    ("executor_disabled", EXECUTOR_DISABLED),
)


def _from_legacy(analysis: Optional[Dict[str, Any]], mapping) -> Optional[str]:
    if not isinstance(analysis, dict):
        return None
    for key, value in mapping:
        if analysis.get(key):
            return value
    return None


def report_state_of(record) -> Optional[str]:
    """Состояние доставки отчёта: колонка, иначе старые ключи analysis."""
    return record.report_state or _from_legacy(record.analysis, _REPORT_LEGACY)


def executor_state_of(record) -> Optional[str]:
    """Состояние исполнения: колонка, иначе старые ключи analysis."""
    return record.executor_state or _from_legacy(record.analysis, _EXECUTOR_LEGACY)


def set_report_state(
    record,
    state: Optional[str],
    *,
    attempts: Optional[int] = None,
    now: Optional[datetime] = None,
) -> None:
    """Записать состояние доставки в колонки.

    JSON-ключи не трогаются: их пишет и читает существующий код доставки, и
    ломать его этой правкой незачем. Колонка становится источником истины для
    поиска, JSON остаётся носителем payload'а.
    """
    record.report_state = state
    if attempts is not None:
        record.report_attempts = attempts
    record.report_updated_at = now or datetime.utcnow()


def set_executor_state(
    record,
    state: Optional[str],
    *,
    claimed_at: Optional[datetime] = None,
) -> None:
    """Записать состояние исполнения; `claimed_at` — момент взятия claim'а.

    Время claim'а лежит отдельной колонкой, чтобы TTL считался сравнением
    дат, а не разбором JSON на каждой проверке.
    """
    record.executor_state = state
    if state == EXECUTOR_IN_FLIGHT:
        record.executor_claimed_at = claimed_at or datetime.utcnow()
    elif state is not None:
        # Терминальное состояние снимает claim: иначе TTL считался бы от
        # момента, который уже ничего не значит.
        record.executor_claimed_at = None


def claim_is_fresh(record, ttl_seconds: int, now: Optional[datetime] = None) -> bool:
    """Жив ли claim исполнителя. Нет claim'а — не жив."""
    if record.executor_state != EXECUTOR_IN_FLIGHT:
        return False
    claimed = record.executor_claimed_at
    if claimed is None:
        # Состояние есть, времени нет — считаем протухшим: иначе застрявшая
        # запись блокировала бы действие навсегда.
        return False
    return (now or datetime.utcnow()) - claimed < _timedelta(ttl_seconds)


def _timedelta(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)
