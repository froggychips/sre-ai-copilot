"""Тесты на app.logger — httpx-loggers silenced (token-leak prevention).

httpx и httpcore по дефолту пишут полный URL в INFO. Webhook URL Discord
содержит bot token в path → попадает в worker stdout. Эти loggers должны
быть переведены в WARNING после configure_logger().
"""
import logging

from app.logger import configure_logger


def test_httpx_loggers_are_silenced_to_warning():
    """После configure_logger httpx/httpcore не должны печатать INFO."""
    # Reset на дефолтные уровни перед тестом — структура imports неопределена.
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    configure_logger()

    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        lg = logging.getLogger(name)
        assert lg.getEffectiveLevel() >= logging.WARNING, (
            f"{name} logger should be at WARNING+, got {lg.getEffectiveLevel()}"
        )


def test_httpx_info_log_is_filtered_by_isEnabledFor():
    """Sanity: проверяем что INFO-уровень для httpx отключён через
    стандартный logging.Logger.isEnabledFor() — это то, что httpx
    реально использует перед записью лога.
    """
    configure_logger()
    httpx_logger = logging.getLogger("httpx")
    assert not httpx_logger.isEnabledFor(logging.INFO)
    assert httpx_logger.isEnabledFor(logging.WARNING)
