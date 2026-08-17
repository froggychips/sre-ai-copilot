"""Периодическая задача обязана протухать, если её не подхватили вовремя.

Замер на проде 08.08.2026: в очереди Celery лежало 230 задач, из них 94
`kg_external_probe` — таск с интервалом в одну минуту, накопленный за полтора
часа. Смысла в этих 94 нет: probe отвечает на вопрос «как дела прямо сейчас»,
и выполнение тика, отправленного 90 минут назад, — чистая трата слота.

Хуже, что очередь съедала воркеров целиком: `kg_topology_resources_sync`
отправлялся по расписанию в :15 и :30, но не выполнился ни разу — стоял за
протухшим хвостом. Синк топологии пришлось запускать отдельным Job-ом руками.

`expires` решает это на уровне брокера: просроченную задачу воркер отбрасывает,
не выполняя. Здесь — guard, чтобы новая периодическая задача не появилась без
него.
"""
from __future__ import annotations

import re

import pytest

# Отчёты — единственное исключение по величине окна: пропущенный дайджест
# означает «отчёта не будет вовсе», поэтому им дают часы, а не минуты.
_REPORT_TASKS = {
    "daily-stats-digest",
    "team-daily-digest",
    "chronic-alerts-digest",
}


def _beat_schedule():
    from app.workers.tasks import celery_app

    return celery_app.conf.beat_schedule


def test_every_periodic_task_has_expires():
    """Ни одной задачи без `expires` — иначе очередь копится молча."""
    missing = [
        name for name, cfg in _beat_schedule().items()
        if not (cfg.get("options") or {}).get("expires")
    ]
    assert not missing, (
        f"периодические задачи без expires: {sorted(missing)}. "
        "Добавь \"options\": {\"expires\": <секунды>} — иначе протухшие тики "
        "будут выполняться спустя часы, занимая воркеров"
    )


@pytest.mark.parametrize("name,cfg", sorted(_beat_schedule().items()))
def test_expires_shorter_than_interval(name, cfg):
    """Окно жизни задачи меньше её интервала.

    Если expires >= интервала, в очереди могут одновременно жить два тика
    одной задачи — накопление возвращается, просто медленнее.
    """
    expires = (cfg.get("options") or {}).get("expires")
    assert expires and expires > 0

    if name in _REPORT_TASKS:
        # Отчёты запускаются раз в сутки/6 часов, окно намеренно широкое.
        assert expires <= 24 * 3600
        return

    schedule = repr(cfg.get("schedule"))
    # Порядок разбора важен: hour="*/N" задаёт интервал в ЧАСАХ и перебивает
    # minute — иначе crontab(minute=43, hour="*/6") прочитается как «раз в
    # 6 минут» вместо «раз в 6 часов».
    hour_m = re.search(r"hour='?\*/(\d+)", schedule)
    minute_m = re.search(r"minute='?\*/(\d+)", schedule)
    # celery печатает crontab как `<crontab: 40 3 * * * (...)>` — минута, час,
    # день. Конкретный час (не `*`, не `*/N`) означает «раз в сутки»; без этой
    # ветки суточная задача читалась бы как ежечасная и требовала expires < 1ч
    # без всякой причины.
    daily_m = re.search(r"<crontab: \d+ (\d+) \* \* \*", schedule)
    if hour_m:
        interval = int(hour_m.group(1)) * 3600
    elif daily_m:
        interval = 24 * 3600
    elif minute_m:
        interval = int(minute_m.group(1)) * 60
    elif re.search(r"minute='?\*'?[,)]", schedule):
        interval = 60
    else:
        # crontab(minute=N) без hour — ежечасно.
        interval = 3600

    assert expires < interval, (
        f"{name}: expires={expires}s не меньше интервала {interval}s — "
        "два тика смогут одновременно ждать в очереди"
    )


# --- залп на ровном часе --------------------------------------------------
#
# Замер 17.08.2026: в `:00` стартовали ОДНОВРЕМЕННО 20 задач из 30, в `:30`
# — 15. Отсюда 14 OOMKill за трое суток: ни одна задача не тяжелее 244 МБ,
# но при `--concurrency=4` залп занимает все форки разом, и пик cgroup
# доходил до 2978 МБ при лимите 3072.
#
# Лечится расписанием: `*/10` у пяти задач означает, что все пять стартуют в
# одну и ту же минуту. Смещения делают из залпа поток.

_MAX_TASKS_PER_MINUTE = 8


def _tasks_by_minute():
    import collections
    import re
    slots = collections.defaultdict(list)
    for name, cfg in _beat_schedule().items():
        m = re.search(r"<crontab: ([^ ]+) ([^ ]+)", repr(cfg.get("schedule")))
        if not m:
            continue
        minute = m.group(1)
        if minute == "*":
            minutes = set(range(60))
        elif minute.startswith("*/"):
            minutes = set(range(0, 60, int(minute[2:])))
        else:
            minutes = {int(x) for x in minute.split(",") if x.isdigit()}
        for mm in minutes:
            slots[mm].append(name)
    return slots


def test_no_minute_starts_a_stampede():
    """Ни одна минута не должна собирать залп задач.

    Воркер работает с `--concurrency=4`: всё, что не влезло, ждёт, а то, что
    влезло, складывает свои пики памяти.
    """
    slots = _tasks_by_minute()
    worst = max(slots.items(), key=lambda kv: len(kv[1]))
    assert len(worst[1]) <= _MAX_TASKS_PER_MINUTE, (
        f"в минуту :{worst[0]:02d} стартуют {len(worst[1])} задач: "
        f"{sorted(worst[1])}. Разведи смещениями — `*/N` у нескольких задач "
        "означает старт в одну и ту же минуту."
    )


def test_hourly_tasks_are_spread_across_the_hour():
    """Задачи с шагом в час не должны липнуть к `:00`."""
    slots = _tasks_by_minute()
    assert len(slots[0]) <= _MAX_TASKS_PER_MINUTE, (
        f"на ровном часе {len(slots[0])} задач — именно так выглядел залп, "
        "приводивший к OOM"
    )
