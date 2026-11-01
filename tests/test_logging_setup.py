"""Настройка логирования ставится в реальных точках входа процессов.

До 1.0.33 `app/logger.py` никто не импортировал: structlog писал DEBUG,
httpx — полные URL (с токеном вебхука Discord). Прежний тест проходил,
потому что звал configure_logger() сам. Поэтому главные проверки здесь — в
отдельном интерпретаторе, только через импорт точки входа.
"""
import logging
import os
import subprocess
import sys

import pytest

from app.observability.logging_setup import configure_logging

_HTTP = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection")

_CHECK = (
    "import logging, sys, structlog; import {module}; "
    "lg = structlog.get_logger('t'); "
    "ok_level = not lg.is_enabled_for(logging.DEBUG) and lg.is_enabled_for(logging.INFO); "
    "ok_http = all(logging.getLogger(n).getEffectiveLevel() >= logging.WARNING "
    "for n in {http!r}); "
    "sys.exit(0 if ok_level and ok_http else 3)"
)


@pytest.mark.parametrize("module", ["app.main", "app.workers.tasks"])
def test_entrypoint_configures_logging_in_clean_process(module):
    env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
    env["LLM_BACKEND"] = "claude_cli"
    r = subprocess.run([sys.executable, "-c", _CHECK.format(module=module, http=_HTTP)],
                       env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]


def test_log_level_env_override(monkeypatch):
    import structlog

    monkeypatch.setenv("LOG_LEVEL", "debug")
    configure_logging()
    assert structlog.get_logger("t").is_enabled_for(logging.DEBUG)
    monkeypatch.setenv("LOG_LEVEL", "nonsense")
    configure_logging()
    lg = structlog.get_logger("t")
    assert not lg.is_enabled_for(logging.DEBUG) and lg.is_enabled_for(logging.INFO)


def test_httpx_loggers_silenced():
    for name in _HTTP:
        logging.getLogger(name).setLevel(logging.NOTSET)
    configure_logging()
    for name in _HTTP:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
