"""Тесты на backpressure-конфигурацию Celery worker'а.

Проверяем что в non-eager режиме применены настройки защиты от:
- flood (prefetch_multiplier=1)
- memory leak (max_tasks_per_child=50)
- зависших задач (time_limit + soft_time_limit)
- LLM-бюджет flood (rate_limit на process_incident)
"""
from unittest.mock import patch


def test_celery_backpressure_config_applied_in_prod_mode():
    """В non-eager mode → backpressure-настройки применены к celery_app."""
    # Свежий импорт с CELERY_TASK_ALWAYS_EAGER=False (default prod)
    import importlib

    import app.workers.tasks as tasks_mod
    with patch.object(tasks_mod.settings, "CELERY_TASK_ALWAYS_EAGER", False):
        importlib.reload(tasks_mod)
        conf = tasks_mod.celery_app.conf
        assert conf.worker_prefetch_multiplier == 1
        assert conf.worker_max_tasks_per_child == 50
        assert conf.task_time_limit == 1800
        assert conf.task_soft_time_limit == 1500
        assert conf.broker_connection_retry_on_startup is True


def test_process_incident_has_rate_limit():
    """process_incident task должен иметь rate_limit для защиты LLM-бюджета."""
    from app.workers.tasks import process_incident_task

    # Celery хранит rate_limit как атрибут task
    assert process_incident_task.rate_limit is not None
    # Default = "30/m"
    assert "/m" in str(process_incident_task.rate_limit)
