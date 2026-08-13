"""Форматтеры дайджеста — таблицей входов и выходов, без БД и Redis.

Слой чистый, поэтому проверяется дёшево и полностью. Главное свойство,
которое здесь охраняется: **None ≠ ноль**. Метрика, которую не получили,
обязана выглядеть как «?», а «0» означает измеренный ноль — путаница между
ними уже приводила к `Crashloops: None` рядом с трендом «40→35», то есть
дайджест противоречил сам себе в двух соседних строках.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.digest.formatting import (fmt_delta_int, fmt_delta_pp,
                                            fmt_firing_series_trend,
                                            fmt_snapshot_metric,
                                            format_gap_minutes,
                                            format_services_list,
                                            health_marker, humanize_ago)


# --- тренд firing-серий ---------------------------------------------------


def test_first_run_is_labelled_not_faked_as_zero():
    """Вчера неизвестно → «new baseline», а не эффектная дельта от нуля."""
    assert fmt_firing_series_trend(673, None) == " (new baseline)"


@pytest.mark.parametrize("today,yesterday,expected", [
    (673, 626, " (+47 vs вчера, +7.5%)"),
    (600, 673, " (-73 vs вчера, -10.8%)"),
    (673, 673, " (=0 vs вчера)"),
])
def test_trend_shows_delta_and_percent(today, yesterday, expected):
    assert fmt_firing_series_trend(today, yesterday) == expected


def test_growth_from_zero_does_not_divide_by_zero():
    assert fmt_firing_series_trend(5, 0) == " (+5 vs вчера)"


# --- дельты ---------------------------------------------------------------


@pytest.mark.parametrize("fn,today,yesterday,expected", [
    (fmt_delta_pp, 72.0, 69.0, "+3pp"),
    (fmt_delta_pp, 69.0, 72.0, "-3pp"),
    (fmt_delta_pp, 69.2, 69.0, "±0pp"),
    (fmt_delta_int, 12, 10, "+2"),
    (fmt_delta_int, 10, 12, "-2"),
    (fmt_delta_int, 10, 10, "±0"),
])
def test_deltas(fn, today, yesterday, expected):
    assert fn(today, yesterday) == expected


@pytest.mark.parametrize("fn", [fmt_delta_pp, fmt_delta_int])
@pytest.mark.parametrize("today,yesterday", [(None, 10), (10, None), (None, None)])
def test_delta_without_both_sides_is_empty(fn, today, yesterday):
    """Не с чем сравнивать — не рисуем ничего, а не «±0»."""
    assert fn(today, yesterday) == ""


# --- метрика снапшота: None ≠ 0 ------------------------------------------


def test_missing_metric_is_a_question_mark():
    assert fmt_snapshot_metric(None) == "?"


def test_measured_zero_stays_zero():
    """0 — это измеренный ноль, и он обязан отличаться от «нет данных»."""
    assert fmt_snapshot_metric(0) == "0"
    assert fmt_snapshot_metric(0.0) == "0"


def test_float_is_rounded_to_int():
    assert fmt_snapshot_metric(41.7) == "42"


# --- маркеры и возраст ----------------------------------------------------


@pytest.mark.parametrize("score,marker", [
    (1.0, "🟢"), (0.7, "🟢"), (0.69, "🟡"), (0.4, "🟡"), (0.39, "🔴"), (0.0, "🔴"),
])
def test_health_marker_thresholds(score, marker):
    assert health_marker(score) == marker


@pytest.mark.parametrize("delta,expected", [
    (timedelta(seconds=5), "5s ago"),
    (timedelta(minutes=5), "5m ago"),
    (timedelta(hours=2), "2h ago"),
    (timedelta(days=3), "3d ago"),
])
def test_humanize_ago(delta, expected):
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    assert humanize_ago((now - delta).isoformat(), now=now) == expected


@pytest.mark.parametrize("value", [None, "", "не дата"])
def test_humanize_ago_degrades_to_question_mark(value):
    assert humanize_ago(value) == "?"


def test_humanize_ago_accepts_zulu_suffix():
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    assert humanize_ago("2026-08-13T11:00:00Z", now=now) == "1h ago"


# --- списки и лаги --------------------------------------------------------


def test_services_list_caps_and_counts_rest():
    names = ["town-service", "chat-tasks-service", "map-service", "a", "b"]
    assert format_services_list(names) == "town-service/chat-tasks-service/map-service +2 more"


def test_services_list_without_rest_has_no_suffix():
    assert format_services_list(["a", "b"]) == "a/b"


def test_empty_services_list_is_question_mark():
    assert format_services_list([]) == "?"


@pytest.mark.parametrize("minutes,expected", [(5, "5m"), (59, "59m"), (60, "1h"), (150, "2h")])
def test_gap_minutes_switches_to_hours(minutes, expected):
    assert format_gap_minutes(minutes) == expected
