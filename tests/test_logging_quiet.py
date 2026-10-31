"""Тихие логи: probe-фильтр uvicorn и отключённый перехват stdout в Celery.

Замер 31.10.2026: 36 тыс. строк `WARNING/ForkPoolWorker` в сутки у воркера
(это были info-строки structlog, завёрнутые Celery) и 34,5 тыс. строк
access-лога /healthz+/readyz у API при ~300 настоящих запросах.
"""
import logging

from app.observability.access_log import HealthProbeAccessFilter


def _record(path: str, status: int, method: str = "GET") -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:1234", method, path, "1.1", status), exc_info=None,
    )


def test_probe_ok_is_dropped():
    f = HealthProbeAccessFilter()
    assert f.filter(_record("/healthz", 200)) is False
    assert f.filter(_record("/readyz", 200)) is False
    assert f.filter(_record("/readyz?verbose=1", 200)) is False


def test_probe_5xx_is_kept():
    f = HealthProbeAccessFilter()
    assert f.filter(_record("/healthz", 503)) is True
    assert f.filter(_record("/readyz", 500)) is True


def test_real_requests_are_kept():
    f = HealthProbeAccessFilter()
    assert f.filter(_record("/webhooks/alertmanager/store", 200, "POST")) is True
    assert f.filter(_record("/healthz-ish", 200)) is True


def test_non_access_shaped_record_passes():
    f = HealthProbeAccessFilter()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain", None, None)
    assert f.filter(rec) is True


def test_api_process_installs_filter_without_app_logger():
    """Фильтр ставит сам `app.main`, в чистом процессе.

    1.0.28: фильтр жил в app/logger.py, который API не импортирует, — в
    проде не ставился, а прежний тест проходил, потому что импортировал
    app.logger сам. Поэтому — отдельный интерпретатор, только `import app.main`.
    """
    import os
    import subprocess
    import sys

    code = (
        "import logging, sys; import app.main; "
        "from app.observability.access_log import HealthProbeAccessFilter as F; "
        "fs = logging.getLogger('uvicorn.access').filters; "
        "sys.exit(0 if sum(isinstance(f, F) for f in fs) == 1 "
        "and 'app.logger' not in sys.modules else 3)"
    )
    env = {**os.environ, "LLM_BACKEND": "claude_cli"}
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_celery_does_not_redirect_stdouts():
    from app.workers.tasks import celery_app

    assert celery_app.conf.worker_redirect_stdouts is False
