"""Фильтр access-лога uvicorn: без успешных probe-запросов kubelet.

kubelet дёргает /healthz и /readyz каждые 5 с на каждой реплике API —
34,5 тыс. строк access-лога в сутки (замер 31.10.2026), в которых тонут
~300 настоящих запросов. Фильтр снимает только успешные probe-ответы:
5xx на probe — это как раз то, что хочется увидеть.

Модуль без побочных эффектов при импорте: ставит фильтр только
`install_probe_access_filter()`, который зовёт `app/main.py`. В 1.0.28 фильтр
жил в `app/logger.py`, который API-процесс не импортирует вовсе (импорт
заодно переключил бы structlog на JSON) — фильтр не ставился, а тест его
не ловил, потому что импортировал `app.logger` сам.
"""
from __future__ import annotations

import logging

_PROBE_PATHS = ("/healthz", "/readyz")


class HealthProbeAccessFilter(logging.Filter):
    """Глушит access-записи uvicorn по /healthz и /readyz со статусом < 500.

    uvicorn.access форматирует запись как
    ``'%s - "%s %s HTTP/%s" %d'`` с args (client, method, path, version, status).
    Любая другая форма записи пропускается без изменений.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        if status >= 500:
            return True
        return not any(path == p or path.startswith(p + "?") for p in _PROBE_PATHS)


def install_probe_access_filter() -> None:
    """Поставить фильтр на `uvicorn.access` (идемпотентно).

    uvicorn настраивает логирование (dictConfig) ДО импорта приложения и
    существующие фильтры логгера не сбрасывает, поэтому достаточно позвать
    это при импорте `app.main`.
    """
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthProbeAccessFilter) for f in access.filters):
        access.addFilter(HealthProbeAccessFilter())
