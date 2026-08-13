"""Учёт секций, не собравшихся в текущем прогоне дайджеста.

Секция, поймавшая исключение, возвращает "" — и пустая секция становится
неотличима от «данных нет». 07.08.2026 из дайджеста так молча пропали два
блока (deploy→incident и beat_heartbeats), причём заметили это глазами, а не
мониторингом. Отсюда правило: дайджест обязан говорить о себе, что он
неполный.

ContextVar, а не модульный список: воркер собирает дайджест конкурентно с
другими задачами, и глобальный список протёк бы между ними.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import List

__all__ = [
    "reset_section_failures",
    "note_section_failure",
    "section_failures_line",
    "failed_sections",
]

_section_failures: ContextVar[List[str]] = ContextVar("digest_section_failures")


def reset_section_failures() -> None:
    """Начать новую сборку дайджеста с чистым списком сбоев."""
    _section_failures.set([])


def note_section_failure(section: str) -> None:
    """Запомнить, что секция не смогла отработать."""
    try:
        failures = _section_failures.get()
    except LookupError:
        failures = []
        _section_failures.set(failures)
    if section not in failures:
        failures.append(section)


def section_failures_line() -> str:
    """Строка со списком недоступных секций (или "").

    СЧИТАЕТСЯ последней (после всех секций), но РЕНДЕРИТСЯ сразу под
    заголовком: читатель должен видеть, что часть картины отсутствует, иначе
    он примет неполный дайджест за полный. Пока строка стояла в самом конце,
    её первой съедала обрезка по лимиту Discord — то есть предупреждение
    исчезало именно тогда, когда дайджест и правда был неполным
    (см. discord.service._truncate_stats_description).
    """
    try:
        failures = _section_failures.get()
    except LookupError:
        return ""
    if not failures:
        return ""
    listed = ", ".join(f"`{f}`" for f in sorted(failures))
    return (
        f"⚠️ **Секции недоступны ({len(failures)}):** {listed} — данные ниже "
        "неполные, смотреть логи воркера по `stats_digest.*_failed`"
    )


def failed_sections() -> List[str]:
    """Список упавших секций текущего прогона (для логов и метаданных сборки)."""
    try:
        return list(_section_failures.get())
    except LookupError:
        return []
