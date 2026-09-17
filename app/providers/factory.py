"""Выбор реализации провайдера по конфигу.

Критерий готовности интерфейса сформулирован так: вызывающий код работает
на любой реализации без изменений, выбор задаётся конфигом. Фабрика — то
место, где это утверждение можно проверить: если для новой реализации
пришлось тронуть что-то выше неё, абстракция протекла.
"""
from __future__ import annotations

from typing import Optional

from app.config import settings
from app.providers.logs import LogProvider

#: Реализации, которые умеет собрать фабрика. ClickHouse появится здесь
#: вторым — и ровно на нём выяснится, годится ли интерфейс: абстракция,
#: выведенная из одной реализации, обычно повторяет её форму.
_LOG_BACKENDS = ("seq",)


def make_log_provider(
    name: str,
    url: str,
    token: Optional[str] = None,
    timeout: float = 10.0,
    backend: Optional[str] = None,
) -> LogProvider:
    """Собрать провайдер логов для одного инстанса.

    `backend` по умолчанию берётся из `settings.LOG_PROVIDER_BACKEND`.
    Неизвестное значение — это ошибка конфигурации, а не повод молча
    подставить Seq: тихий фолбэк на «что-то работающее» означал бы, что
    опечатка в настройке выглядит как рабочая система.
    """
    chosen = (backend or getattr(settings, "LOG_PROVIDER_BACKEND", "seq") or "seq").lower()
    if chosen == "seq":
        from app.providers.seq_logs import SeqLogProvider

        return SeqLogProvider(name=name, base_url=url, api_key=token, timeout=timeout)
    raise ValueError(
        f"неизвестный LOG_PROVIDER_BACKEND={chosen!r}; "
        f"поддерживаются: {', '.join(_LOG_BACKENDS)}"
    )
