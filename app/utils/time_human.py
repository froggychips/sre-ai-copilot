"""Human-readable «X ago» форматирование длительностей.

Чистые функции. БЕЗ I/O. Используется в discord embed builder, чтобы
on-call инженер не парсил «2778 мин назад» в уме (это 1.9 суток).

Правила (см. on-call feedback 10:38):
    * `< 60 мин` → `42 min ago` (минуты как есть)
    * `60..23*60` → `4.3h ago` (часы с одним десятичным знаком)
    * `24*60..30*24*60` → `1.9d ago` / `7d ago` (дни)
    * `>= 30*24*60` → `5w ago` (недели)
    * `<= 0` → `just now` (защита от clock-skew / negative deltas)

Ноль/негатив — `just now` (не «-3 мин назад»: это путает).
"""
from __future__ import annotations


def humanize_minutes_ago(minutes: int | float | None) -> str:
    """Сжать `minutes_before` в одну human-readable строку.

    `None` → "?", чтобы старый код, который форматит `f"{mins} мин назад"`
    с `mins="?"`, мог быть заменён на `humanize_minutes_ago(mins)` без
    отдельной ветки на None.
    """
    if minutes is None:
        return "?"
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "?"
    if m <= 0:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    hours = m / 60.0
    if hours < 24:
        # 1.0h..23.9h — один знак после точки.
        return f"{hours:.1f}h ago"
    days = m / (60.0 * 24.0)
    if days < 30:
        # 1.0d..29.x d
        # < 7 дней — десятичный (1.9d), >= 7 — целый (12d), читается лучше.
        if days < 7:
            return f"{days:.1f}d ago"
        return f"{int(days)}d ago"
    weeks = int(days // 7)
    return f"{weeks}w ago"


def humanize_seconds_ago(seconds: int | float | None) -> str:
    """То же, но из секунд. Просто перегон в минуты."""
    if seconds is None:
        return "?"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s <= 0:
        return "just now"
    if s < 60:
        return f"{int(s)}s ago"
    return humanize_minutes_ago(int(s / 60))
