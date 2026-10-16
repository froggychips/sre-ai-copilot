"""Инвариант: лок задачи после смерти воркера живёт меньше её интервала.

16.10.2026: у kg_topology_sync (crontab minute=0) лок был на 3600 с. Деплой
в 08:01 убил прогон 08:00, лок остался, и запуск 09:00:00 получил
`already_running` — потерян час при времени синка ~2 мин. Так было у 10 из 17
задач с локом. Теперь лок держится арендой (task_lock.LEASE_SECONDS), и этот
тест не даёт вернуть дефект ни новой задачей, ни частым расписанием.
"""
from datetime import timedelta

import pytest
from celery.schedules import crontab, schedule

from app.workers.tasks import celery_app


def _min_interval_seconds(sched) -> float:
    if isinstance(sched, crontab):
        # Минуты недели, когда срабатывает; минимальный зазор с переходом
        # через конец недели. day_of_month/month_of_year у нас не используются.
        points = sorted(
            d * 1440 + h * 60 + m
            for d in sched.day_of_week for h in sched.hour for m in sched.minute
        )
        gaps = [b - a for a, b in zip(points, points[1:])]
        gaps.append(points[0] + 7 * 1440 - points[-1])
        return min(gaps) * 60.0
    if isinstance(sched, schedule):
        return sched.run_every.total_seconds()
    if isinstance(sched, timedelta):
        return sched.total_seconds()
    return float(sched)


def _locked_beat_tasks():
    out = []
    for entry, cfg in celery_app.conf.beat_schedule.items():
        task = celery_app.tasks.get(cfg["task"])
        run = getattr(task, "run", None)
        lease = getattr(run, "lock_lease_seconds", None)
        if lease is not None:
            out.append((entry, cfg["task"], lease, _min_interval_seconds(cfg["schedule"])))
    return out


def test_there_are_locked_beat_tasks():
    """Иначе инвариант ниже молча проверял бы пустоту."""
    assert len(_locked_beat_tasks()) >= 10


@pytest.mark.parametrize("entry,task,lease,interval", _locked_beat_tasks())
def test_lock_after_worker_death_is_shorter_than_interval(entry, task, lease, interval):
    assert lease < interval, (
        f"{task}: лок после смерти воркера живёт {lease} с при интервале "
        f"{interval:.0f} с — смерть воркера съест следующий запуск"
    )


def test_interval_parser():
    assert _min_interval_seconds(crontab(minute=0)) == 3600
    assert _min_interval_seconds(crontab(minute="15,45")) == 1800
    assert _min_interval_seconds(crontab(minute="*/10")) == 600
    assert _min_interval_seconds(crontab(minute=43, hour="*/6")) == 6 * 3600
    assert _min_interval_seconds(crontab(minute="1,11,21,31,41,51")) == 600
    assert _min_interval_seconds(timedelta(minutes=7)) == 420
