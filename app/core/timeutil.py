"""Единый разбор и нормализация времени. Контракт: внутри системы — naive UTC.

Зачем один модуль. К 20.08.2026 в проекте было ПЯТЬ независимых реализаций
`_parse_ts` (auto_populator, namespace_lifecycle, incident_ctx, recent_deploy,
clickhouse_service) и ДВЕ копии `_ensure_naive` (queries, stale_classifier).
Причём пять `_parse_ts` возвращали разные типы: часть naive, часть aware,
одна — «как пришло». Смешивание naive и aware в Python даёт либо TypeError,
либо молчаливый сдвиг, и второе хуже.

Проверка показала, что действующего бага там нет: TeamCity-время нормализует
`_tc_to_iso` (приводит к UTC и ставит `Z`), поэтому голый
`.replace(tzinfo=None)` ниже по потоку срабатывает верно. Но верно он
срабатывает СЛУЧАЙНО — ровно до первого источника, который отдаст время со
смещением. Такой источник в проекте уже был: AlertManager-webhook присылает
`startsAt` с `+03:00`, и обрезание tzinfo сдвигало окно поиска на три часа —
«deploy-атрибуция и pod trail молча смотрели не туда» (комментарий в
`queries._ensure_naive`, оставленный после того инцидента).

Контракт здесь один и явный:

    parse_ts()    → naive UTC или None
    ensure_naive() → naive UTC (приводит через astimezone, не обрезает)
    ensure_aware() → aware UTC

Почему naive, а не aware: все timestamp-колонки KG объявлены как
`Column(DateTime)` без timezone, то есть база хранит naive. Выбор формата
внутри системы должен совпадать с форматом хранения, иначе конвертация
всплывает в случайных местах.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

#: Дробная часть секунд длиннее шести знаков. `fromisoformat` такую не
#: принимает, а Alertmanager её присылает: из-за этого его `startsAt` однажды
#: не парсился вообще, функция возвращала None, и blast radius молча
#: выключался почти на каждом алерте (см. clickhouse_service._parse_ts).
_FRACTION_RE = re.compile(r"\.(\d{7,})")

__all__ = ["parse_ts", "ensure_naive", "ensure_aware", "utcnow"]


def ensure_naive(dt: datetime) -> datetime:
    """Привести к naive UTC. Смещение УЧИТЫВАЕТСЯ, а не отбрасывается.

    Разница принципиальная: `.replace(tzinfo=None)` у `12:00+03:00` даёт
    `12:00`, а правильный ответ — `09:00`. Именно так однажды и сдвинулось
    окно поиска на три часа.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def ensure_aware(dt: datetime) -> datetime:
    """Привести к aware UTC. Naive считается UTC — так его и хранят."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    """Текущее время в том же формате, что и всё остальное: naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(raw: Any) -> Optional[datetime]:
    """Разобрать время из внешнего источника → naive UTC. None при мусоре.

    Принимает и строку, и готовый datetime: источники разнородны (kubectl
    отдаёт строку, ORM — объект), и вызывающему не приходится это различать.

    `Z` нормализуется к `+00:00` до разбора: `fromisoformat` не понимал `Z`
    до Python 3.11, а поддерживать обе версии дешевле, чем помнить, на какой
    из них запущен воркер.

    Возвращает NAIVE намеренно — см. контракт в докстринге модуля. Если
    вызывающему нужен aware, он оборачивает результат в `ensure_aware`, и в
    коде видно, что тип меняется осознанно.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return ensure_naive(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # `Z` → смещение: fromisoformat не понимал `Z` до Python 3.11.
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    else:
        s = s.replace("Z", "+00:00")
    # Микросекунды обрезаем до шести знаков — см. _FRACTION_RE.
    s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6], s)
    try:
        return ensure_naive(datetime.fromisoformat(s))
    except (ValueError, AttributeError):
        return None
