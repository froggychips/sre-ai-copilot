"""Состояние обработки: колонки как источник истины, JSON как хвост.

В `incidents.analysis` жила машина состояний из девяти ключей, и два из них
были координационными примитивами: outbox доставки отчёта и claim
исполнителя с TTL. Координация в JSON плоха тремя вещами сразу — поиск
полным сканом, невидимость состояния в схеме и read-modify-write там, где
хватило бы обычного UPDATE.

Миграция 20260819_0200 вынесла состояние в колонки, оставив payload в JSON.
Линия раздела: «по чему ищут и координируются» против «что показывают».

Записи, сделанные до миграции, должны продолжать читаться — иначе выкат и
миграция оказались бы связаны порядком.
"""
from datetime import datetime, timedelta

import pytest

from app.database import IncidentRecord
from app.services.incident_state import (EXECUTOR_APPLIED, EXECUTOR_IN_FLIGHT,
                                         REPORT_FAILED, REPORT_PENDING,
                                         REPORT_SENT, claim_is_fresh,
                                         executor_state_of, report_state_of,
                                         set_executor_state, set_report_state)

NOW = datetime(2026, 8, 19, 12, 0)


def _record(**kwargs) -> IncidentRecord:
    return IncidentRecord(incident_id="inc-1", status="new", **kwargs)


# --- колонка выигрывает у JSON --------------------------------------------


def test_column_is_the_source_of_truth():
    rec = _record(report_state=REPORT_SENT,
                  analysis={"report_pending": {"attempts": 3}})
    assert report_state_of(rec) == REPORT_SENT


def test_legacy_json_is_read_when_column_is_empty():
    """Записи до миграции обязаны читаться — иначе выкат связан с миграцией."""
    rec = _record(analysis={"report_pending": {"attempts": 1}})
    assert report_state_of(rec) == REPORT_PENDING


def test_terminal_legacy_state_wins_over_pending():
    """В старом JSON могли остаться оба ключа: отправлено важнее ожидания."""
    rec = _record(analysis={"report_pending": {}, "report_sent": {"attempts": 2}})
    assert report_state_of(rec) == REPORT_SENT


def test_no_state_at_all_is_none():
    """NULL значит «стадии не было» — это не то же, что «была и закончилась»."""
    assert report_state_of(_record()) is None
    assert executor_state_of(_record(analysis={})) is None


@pytest.mark.parametrize("key,expected", [
    ("executor_applied", EXECUTOR_APPLIED),
    ("executor_in_flight", EXECUTOR_IN_FLIGHT),
])
def test_executor_legacy_keys_map_to_states(key, expected):
    assert executor_state_of(_record(analysis={key: {"any": "payload"}})) == expected


def test_broken_analysis_does_not_raise():
    """`analysis` может оказаться не словарём — падать на этом нельзя."""
    assert report_state_of(_record(analysis="мусор")) is None


# --- запись ---------------------------------------------------------------


def test_set_report_state_records_attempts_and_time():
    rec = _record()
    set_report_state(rec, REPORT_FAILED, attempts=3, now=NOW)

    assert rec.report_state == REPORT_FAILED
    assert rec.report_attempts == 3
    assert rec.report_updated_at == NOW


def test_claim_records_its_moment():
    """TTL считается сравнением дат, а не разбором JSON на каждой проверке."""
    rec = _record()
    set_executor_state(rec, EXECUTOR_IN_FLIGHT, claimed_at=NOW)
    assert rec.executor_claimed_at == NOW


def test_terminal_state_clears_the_claim():
    """Иначе TTL считался бы от момента, который уже ничего не значит."""
    rec = _record(executor_state=EXECUTOR_IN_FLIGHT, executor_claimed_at=NOW)
    set_executor_state(rec, EXECUTOR_APPLIED)
    assert rec.executor_claimed_at is None


# --- свежесть claim'а -----------------------------------------------------


def test_fresh_claim_blocks_a_second_apply():
    rec = _record(executor_state=EXECUTOR_IN_FLIGHT,
                  executor_claimed_at=NOW - timedelta(seconds=60))
    assert claim_is_fresh(rec, ttl_seconds=600, now=NOW)


def test_expired_claim_lets_the_action_through():
    rec = _record(executor_state=EXECUTOR_IN_FLIGHT,
                  executor_claimed_at=NOW - timedelta(seconds=1200))
    assert not claim_is_fresh(rec, ttl_seconds=600, now=NOW)


def test_claim_without_timestamp_is_treated_as_expired():
    """Состояние есть, времени нет — иначе запись блокировала бы навсегда."""
    rec = _record(executor_state=EXECUTOR_IN_FLIGHT, executor_claimed_at=None)
    assert not claim_is_fresh(rec, ttl_seconds=600, now=NOW)


def test_no_claim_is_not_fresh():
    assert not claim_is_fresh(_record(), ttl_seconds=600, now=NOW)
