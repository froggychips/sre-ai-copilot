"""Единый разбор времени: контракт «внутри системы — naive UTC».

К 20.08.2026 в проекте было ПЯТЬ независимых реализаций `_parse_ts` и ДВЕ
копии `_ensure_naive`. Пять возвращали разные типы: часть naive, часть aware,
одна — «как пришло». Смешивание naive и aware даёт либо TypeError, либо
молчаливый сдвиг, и второе хуже.

Действующего бага там не было — TeamCity-время нормализует `_tc_to_iso`, и
голое обрезание tzinfo ниже по потоку срабатывало верно. Но верно оно
срабатывало СЛУЧАЙНО, ровно до первого источника со смещением. Такой
источник в проекте уже был: AlertManager присылает `startsAt` с `+03:00`, и
обрезание tzinfo сдвигало окно поиска на три часа.

Здесь закреплены оба урока: и правильное приведение смещения, и обрезание
дробной части, из-за отсутствия которого однажды молча отключался blast
radius.
"""
from datetime import datetime, timezone

import pytest

from app.core.timeutil import ensure_aware, ensure_naive, parse_ts, utcnow


# --- главный урок: смещение учитывается, а не отбрасывается --------------


def test_offset_is_converted_not_truncated():
    """`12:00+03:00` — это `09:00` UTC, а не `12:00`.

    Именно эта разница сдвигала окно поиска на три часа: deploy-атрибуция и
    pod trail молча смотрели не туда.
    """
    assert parse_ts("2026-08-20T12:00:00+03:00") == datetime(2026, 8, 20, 9, 0)


def test_negative_offset_too():
    assert parse_ts("2026-08-20T12:00:00-05:00") == datetime(2026, 8, 20, 17, 0)


def test_zulu_is_utc():
    assert parse_ts("2026-08-20T12:00:00Z") == datetime(2026, 8, 20, 12, 0)


def test_naive_input_is_treated_as_utc():
    """Naive на входе = UTC: в этом виде время хранит база."""
    assert parse_ts("2026-08-20T12:00:00") == datetime(2026, 8, 20, 12, 0)


# --- дробная часть: из-за неё отключался blast radius --------------------


def test_long_fraction_is_trimmed_not_rejected():
    """Alertmanager присылает больше шести знаков; раньше это давало None."""
    got = parse_ts("2026-08-20T12:00:00.123456789Z")
    assert got is not None, "длинная дробная часть снова не парсится"
    assert got.microsecond == 123456


def test_normal_fraction_survives():
    assert parse_ts("2026-08-20T12:00:00.123Z").microsecond == 123000


# --- контракт результата -------------------------------------------------


@pytest.mark.parametrize("raw", [
    "2026-08-20T12:00:00Z",
    "2026-08-20T12:00:00+03:00",
    "2026-08-20T12:00:00",
    datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 20, 12, 0),
])
def test_result_is_always_naive(raw):
    """Один тип на все источники — иначе вычитание двух значений упадёт.

    В `recent_deploy` есть `delta = incident_at - d_ts`: оба через parse_ts,
    и если бы типы разъезжались, это был бы TypeError на живом инциденте.
    """
    got = parse_ts(raw)
    assert got is not None and got.tzinfo is None


def test_datetime_input_is_accepted():
    """Источники разнородны: kubectl отдаёт строку, ORM — объект."""
    assert parse_ts(datetime(2026, 8, 20, 12, 0)) == datetime(2026, 8, 20, 12, 0)


@pytest.mark.parametrize("raw", [None, "", "не время", 42, [], {}])
def test_garbage_gives_none_not_exception(raw):
    """Мусор не должен ронять синк — источники внешние и неаккуратные."""
    assert parse_ts(raw) is None


# --- явные преобразования ------------------------------------------------


def test_ensure_naive_converts_offset():
    aware = datetime(2026, 8, 20, 12, 0, tzinfo=timezone(timezone.utc.utcoffset(None)))
    assert ensure_naive(aware).tzinfo is None


def test_ensure_naive_is_idempotent():
    naive = datetime(2026, 8, 20, 12, 0)
    assert ensure_naive(naive) == naive


def test_ensure_aware_marks_naive_as_utc():
    got = ensure_aware(datetime(2026, 8, 20, 12, 0))
    assert got.tzinfo is not None and got.utcoffset().total_seconds() == 0


def test_ensure_aware_keeps_existing_zone():
    aware = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert ensure_aware(aware) is aware


def test_utcnow_matches_the_storage_format():
    """`utcnow` должен давать тот же вид, что и всё остальное."""
    assert utcnow().tzinfo is None


# --- копий больше быть не должно -----------------------------------------


def test_no_local_parse_ts_implementations_remain():
    """Пятая копия появлялась именно так: «здесь нужно чуть иначе».

    Если поведение действительно другое — это обёртка над общим разбором с
    объяснением, как в clickhouse_service, а не своя реализация.
    """
    import pathlib
    import re

    app = pathlib.Path(__file__).parent.parent / "app"
    own = []
    for path in app.rglob("*.py"):
        if path.name == "timeutil.py":
            continue
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"def _parse_ts\(.*?(?=\ndef |\nclass |\Z)", src, re.S):
            body = m.group(0)
            # Обёртка допустима: она делегирует общему parse_ts.
            if "parse_ts(raw)" not in body and "parse_ts(value)" not in body:
                own.append(path.name)
    assert not own, f"своя реализация разбора времени в: {sorted(set(own))}"
