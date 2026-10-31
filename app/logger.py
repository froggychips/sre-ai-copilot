import logging

import structlog

# httpx / httpcore по дефолту пишут каждый HTTP-запрос INFO-логом с полным
# URL. Это означает что webhook-URL (Discord bot token в path!) и другие
# чувствительные query-params попадают в stdout worker'а:
#
#   INFO/ForkPoolWorker-4] HTTP Request: POST https://discord.com/api/webhooks/<id>/<TOKEN> "HTTP/1.1 204 No Content"
#
# Переводим оба в WARNING — реальные network-ошибки сохранятся (4xx/5xx —
# их httpx уже логирует через RAISE_FOR_STATUS / connection errors), а
# noise + token-leak пропадает.
_SENSITIVE_HTTP_LOGGERS = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection")

# kubelet дёргает /healthz и /readyz каждые 5 с на каждой реплике API —
# 34,5 тыс. строк access-лога в сутки (замер 31.10.2026), в которых тонут
# 300 настоящих запросов. Фильтр снимает только успешные probe-ответы:
# 5xx на probe — это как раз то, что хочется увидеть.
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


def configure_logger():
    """
    Configures structlog to output JSON lines with standard fields.
    Fields: timestamp, level, logger, request_id, message, extras.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    for name in _SENSITIVE_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthProbeAccessFilter) for f in access.filters):
        access.addFilter(HealthProbeAccessFilter())


configure_logger()
logger = structlog.get_logger()
