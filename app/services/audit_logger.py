"""Audit log — отдельный JSON-stream для security/compliance событий.

Поведение:
  * По умолчанию пишет JSON-строки в stdout (Kubernetes стандарт — пусть
    Fluent Bit/Loki/ELK подхватят и положат в централизованный sink).
  * Если задан AUDIT_LOG_PATH и путь не равен "-"/"stdout", пишем в файл.
    Полезно для локального e2e в `/tmp/...`. В prod-кластере с
    readOnlyRootFilesystem=true любые файловые пути сразу упадут — это
    осознанно: stdout есть единственный prod-acceptable вариант.
  * Audit-logger НЕ перенастраивает глобальную structlog-конфигурацию;
    application-логи продолжают идти как раньше.
"""
import sys
from datetime import datetime, timezone
from typing import IO

import structlog

from app.config import settings


def _open_audit_sink() -> IO[str]:
    path = (settings.AUDIT_LOG_PATH or "").strip()
    if not path or path in ("-", "stdout"):
        return sys.stdout
    # buffering=1 — line-buffered: каждая запись фьюшится сразу, чтобы
    # коллектор подхватывал в реальном времени.
    return open(path, "a", buffering=1)


_audit_sink = _open_audit_sink()

_audit_factory = structlog.WriteLoggerFactory(file=_audit_sink)
_audit_processors = [
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.JSONRenderer(),
]

audit_logger = structlog.wrap_logger(
    _audit_factory(),
    processors=_audit_processors,
).bind(stream="sre_audit")


class AuditService:
    def log_event(self, event_type: str, details: dict) -> None:
        audit_logger.info(
            event_type,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            **details,
        )


audit_service = AuditService()
