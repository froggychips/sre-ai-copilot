"""Ожидаемый интервал в sync_lag обязан совпадать с расписанием beat.

Иначе проверка врёт в одну из двух сторон, и обе плохи: заниженный интервал
даёт постоянный warn на здоровом синке, завышенный прячет настоящее
отставание.

Замер 21.08.2026: `kg_nats_subjects_sync` шёл раз в шесть часов
(crontab(minute=43, hour="*/6")), а проверка ожидала прогона каждый час, и
`lag=150.6` у совершенно здорового синка читался как warn. Сверять это
глазами при 31 задаче в расписании — вопрос времени до следующего расхождения.
"""
import pytest

from app.knowledge_graph.self_health import _SYNC_LAG_TARGETS
from app.workers.tasks import celery_app


def _beat_schedule_by_task():
    return {
        cfg["task"]: cfg["schedule"]
        for cfg in celery_app.conf.beat_schedule.values()
    }


def _period_minutes(cron) -> int:
    """Грубый период crontab: достаточно, чтобы поймать разницу в разы."""
    minutes = sorted(cron.minute)
    hours = sorted(cron.hour)
    if len(hours) >= 24:                      # каждый час
        return 60 // len(minutes) if len(minutes) > 1 else 60
    if len(hours) == 1:                       # раз в сутки
        return 1440
    return (24 // len(hours)) * 60            # раз в N часов


@pytest.mark.parametrize("task", sorted(_SYNC_LAG_TARGETS))
def test_sync_lag_intervals_match_beat_schedule(task):
    """Проверка не должна ждать прогонов чаще, чем задача реально ходит."""
    cfg = _SYNC_LAG_TARGETS[task]
    expected = cfg.get("interval_minutes")
    assert expected and expected > 0, f"{task}: интервал не задан"

    schedule = _beat_schedule_by_task().get(task)
    assert schedule is not None, (
        f"{task} перечислен в sync_lag, но отсутствует в beat_schedule — "
        "проверка ждёт прогонов задачи, которая не запускается"
    )

    real = _period_minutes(schedule)
    assert expected >= real, (
        f"{task}: sync_lag ждёт прогон каждые {expected} мин, а задача ходит "
        f"раз в {real} мин. Порог warn = 2×interval, fail = 5×interval — "
        "проверка будет держаться в warn на здоровом синке"
    )


def test_every_sync_lag_target_is_actually_scheduled():
    """Обратная сторона: проверять свежесть незапускаемой задачи бессмысленно."""
    scheduled = set(_beat_schedule_by_task())
    orphans = sorted(set(_SYNC_LAG_TARGETS) - scheduled)
    assert not orphans, f"в sync_lag есть задачи вне расписания: {orphans}"
