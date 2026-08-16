"""Форк воркера обязан перезапускаться по памяти, а не только по счётчику.

Прецедент 15–16.08.2026: 14 OOMKill за двое суток при лимите пода 3Gi.
Причина оказалась не в отдельной задаче — замеры пика RSS на проде:

    kg_endpoints_sync             122 МБ
    kg_metrics_sync               199 МБ
    kg_topology_sync              229 МБ
    k8s_topology_resources_sync   244 МБ

Ни одна не тяжёлая. Но воркер работает с `--concurrency=4`, Python не
возвращает память ОС, а recycle форка шёл ТОЛЬКО по счётчику задач
(`max_tasks_per_child=50`). База четырёх форков в покое доходила до
306/242/213/175 МБ, и каждая тяжёлая задача добавляла сверху свои 200-250.

`worker_max_memory_per_child` при этом не был задан вовсе.
"""
import pytest

from app.config import settings

#: Из деплоя: k8s/worker.yaml → resources.limits.memory
POD_LIMIT_MB = 3072
#: --concurrency=4 в args воркера
FORKS = 4
#: RSS мастер-процесса, замер на проде 16.08.2026
MASTER_MB = 172
#: Пик самой тяжёлой задачи (k8s_topology_resources_sync), замер там же
HEAVIEST_TASK_MB = 250


def test_memory_recycle_is_configured():
    """Без этого форк живёт до 50 задач независимо от того, во что вырос."""
    assert settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB > 0


def test_all_forks_at_the_limit_still_fit_the_pod():
    """Арифметика, которой не хватало: 4 форка на потолке + пики + мастер.

    Если этот тест падает, значит либо потолок форка подняли, либо
    concurrency, и под снова начнёт уходить в OOM.
    """
    base_mb = settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB / 1024
    worst_case = FORKS * (base_mb + HEAVIEST_TASK_MB) + MASTER_MB
    assert worst_case < POD_LIMIT_MB, (
        f"худший случай {worst_case:.0f} МБ не влезает в лимит пода "
        f"{POD_LIMIT_MB} МБ — вернутся OOMKill"
    )


def test_limit_leaves_room_for_the_heaviest_task():
    """Потолок ниже пика тяжёлой задачи заставлял бы recycle после каждой."""
    base_mb = settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB / 1024
    assert base_mb > HEAVIEST_TASK_MB, (
        "потолок форка ниже пика одной задачи: recycle будет срабатывать "
        "постоянно, и воркер займётся перезапусками вместо работы"
    )


def test_task_count_recycle_remains_as_second_guard():
    """Лимит по памяти не отменяет счётчик: утечка может быть медленной."""
    assert settings.CELERY_WORKER_MAX_TASKS_PER_CHILD > 0


@pytest.mark.skipif(settings.CELERY_TASK_ALWAYS_EAGER,
                    reason="в eager-режиме backpressure не применяется")
def test_setting_reaches_celery_config():
    """Значение в config бесполезно, если его не передали в celery."""
    from app.workers.tasks import celery_app
    assert (celery_app.conf.worker_max_memory_per_child
            == settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB)
