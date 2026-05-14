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


configure_logger()
logger = structlog.get_logger()
