"""Настройка логирования процесса: уровень structlog и тишина httpx.

До 1.0.33 её не было ни в одном процессе: `app/logger.py` никто не
импортировал, structlog работал с умолчаниями — уровень DEBUG. Замер
01.11.2026: у copilot-worker-sync 25,5 тыс. из 26,8 тыс. строк за сутки —
debug `kg.service_upserted`. Хуже того, httpx оставался на INFO и писал
полный URL каждого запроса, включая URL вебхука Discord с токеном в пути
(copilot-worker, 31.10 18:33).

Формат вывода НЕ меняется (консольный рендерер structlog по умолчанию):
переход на JSON — отдельное решение, на текущий формат могут опираться
разборщики логов. Меняются только уровень и шумные логгеры.

Ставится явно: `app/main.py` (API) и `app/workers/tasks.py` (все процессы
Celery: worker, worker-sync, executor, beat). Уровень — `LOG_LEVEL`
(по умолчанию INFO).
"""
from __future__ import annotations

import logging
import os

import structlog

# httpx/httpcore пишут каждый запрос INFO-строкой с полным URL: у Discord
# токен вебхука лежит в пути, у части API — в query.
_SENSITIVE_HTTP_LOGGERS = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection")


def _level_from_env() -> int:
    name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def configure_logging() -> None:
    """Идемпотентно: повторный вызов только переустанавливает то же самое."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(_level_from_env()),
    )
    for name in _SENSITIVE_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
