"""EMPTY ≠ SUCCESS: контракт ответа источника.

До 05.09.2026 надзор различал два исхода — Celery SUCCESS и FAILURE. Всё
между ними («ходил, но ничего не получил», «ответила половина инстансов»,
«выключен флагом») сливалось в SUCCESS: задача завершилась без исключения,
heartbeat записан, `check_sync_lag` показывает `ok`.

Так `kg_statics_versions_sync` пятнадцать суток отдавал `observed=0`, а
`kg_seq_logs_sync` 20.08.2026 двенадцать часов — `rows=0`. Оба «успешно».
#350 закрыл это для одной задачи; здесь — правило для всех.
"""
from __future__ import annotations

import pytest

from app.knowledge_graph.source_status import (HEARTBEAT_STATUSES,
                                               SOURCE_STATUS_KEY, SourceStatus,
                                               mark, status_from_counts,
                                               status_of)


# ── status_from_counts ──────────────────────────────────────────────────────

def test_data_present_is_success():
    assert status_from_counts({"fetched": 12}, ("fetched",)) is SourceStatus.SUCCESS


def test_zero_observed_is_empty_not_success():
    """Источник ответил нулём объектов — это событие, а не тишина."""
    assert status_from_counts({"fetched": 0, "inserted": 0}, ("fetched",)) is SourceStatus.EMPTY


def test_missing_counter_counts_as_zero():
    assert status_from_counts({"inserted": 5}, ("fetched",)) is SourceStatus.EMPTY


def test_error_marker_is_failed():
    assert status_from_counts({"error": "boom", "fetched": 3}, ("fetched",)) is SourceStatus.FAILED
    assert status_from_counts({"status": "error"}, ("fetched",)) is SourceStatus.FAILED


def test_skipped_is_unavailable():
    """Выключен флагом / не настроен — до источника не дошли, судить нечего."""
    assert status_from_counts({"skipped": "disabled"}, ("fetched",)) is SourceStatus.UNAVAILABLE


def test_custom_unavailable_flags():
    r = {"data_unavailable": True, "inserted": 0}
    assert status_from_counts(
        r, ("inserted",), unavailable_keys=("data_unavailable",),
    ) is SourceStatus.UNAVAILABLE


def test_errors_with_data_is_partial():
    """Часть запросов упала, но данные есть — не SUCCESS и не FAILED."""
    assert status_from_counts({"fetched": 40, "errors": 3}, ("fetched",)) is SourceStatus.PARTIAL


def test_observed_counts_collections_and_bools():
    assert status_from_counts({"items": [1, 2]}, ("items",)) is SourceStatus.SUCCESS
    assert status_from_counts({"reached": True}, ("reached",)) is SourceStatus.SUCCESS
    assert status_from_counts({"items": []}, ("items",)) is SourceStatus.EMPTY


def test_non_dict_result_is_invalid():
    assert status_from_counts(None, ("fetched",)) is SourceStatus.INVALID
    assert status_from_counts("ok", ("fetched",)) is SourceStatus.INVALID


# ── mark / status_of ────────────────────────────────────────────────────────

def test_mark_writes_serializable_value():
    """retval уезжает в result backend как JSON — enum там не переживёт."""
    r = mark({"fetched": 1}, SourceStatus.SUCCESS)
    assert r[SOURCE_STATUS_KEY] == "success"
    assert isinstance(r[SOURCE_STATUS_KEY], str)


def test_status_of_round_trip():
    assert status_of(mark({}, SourceStatus.PARTIAL)) is SourceStatus.PARTIAL


def test_status_of_unknown_value_is_invalid_not_none():
    """Битое значение — не «контракта нет», а «контракт нарушен»."""
    assert status_of({SOURCE_STATUS_KEY: "weird"}) is SourceStatus.INVALID


def test_status_of_without_key_is_none():
    assert status_of({"fetched": 1}) is None
    assert status_of(None) is None


# ── правило heartbeat ───────────────────────────────────────────────────────

def test_only_success_and_partial_earn_heartbeat():
    """EMPTY — не подтверждение здоровья, и это вся суть контракта."""
    assert HEARTBEAT_STATUSES == {SourceStatus.SUCCESS, SourceStatus.PARTIAL}
    assert SourceStatus.EMPTY not in HEARTBEAT_STATUSES


class _Recorder:
    def __init__(self):
        self.heartbeats: list[str] = []
        self.statuses: list[tuple[str, str]] = []


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    import app.services.digest.state as state
    import app.services.stats_digest as sd
    monkeypatch.setattr(sd, "_record_task_heartbeat", lambda t: rec.heartbeats.append(t))
    monkeypatch.setattr(state, "record_task_status", lambda t, s: rec.statuses.append((t, s)))
    return rec


def _postrun(retval, task="kg_metrics_sync", state="SUCCESS"):
    from types import SimpleNamespace

    from app.workers.tasks import _record_beat_heartbeat
    _record_beat_heartbeat(sender=task, task=SimpleNamespace(name=task),
                           state=state, retval=retval)


def test_heartbeat_written_on_success(recorder):
    _postrun(mark({"fetched": 3}, SourceStatus.SUCCESS))
    assert recorder.heartbeats == ["kg_metrics_sync"]
    assert recorder.statuses == [("kg_metrics_sync", "success")]


def test_heartbeat_written_on_partial(recorder):
    _postrun(mark({"fetched": 3, "errors": 1}, SourceStatus.PARTIAL))
    assert recorder.heartbeats == ["kg_metrics_sync"]


@pytest.mark.parametrize("status", [
    SourceStatus.EMPTY, SourceStatus.UNAVAILABLE,
    SourceStatus.FAILED, SourceStatus.INVALID,
])
def test_heartbeat_withheld_but_status_recorded(recorder, status):
    """Прогон был, ответа по существу нет: статус пишем, heartbeat — нет.

    По статусу sync_lag отличит «не ходил» от «ходил и возвращался с
    пустыми руками» — второе и есть картина 05.09.2026.
    """
    _postrun(mark({"fetched": 0}, status))
    assert recorder.heartbeats == []
    assert recorder.statuses == [("kg_metrics_sync", status.value)]


def test_legacy_retval_without_contract_still_works(recorder):
    """Задача без контракта — старое правило по error-маркерам, с warning."""
    _postrun({"fetched": 3})
    assert recorder.heartbeats == ["kg_metrics_sync"]
    _postrun({"error": "x"})
    assert recorder.heartbeats == ["kg_metrics_sync"]  # второй не добавился


def test_lock_skip_writes_neither(recorder):
    """Пропуск по singleton-локу — не прогон и не ответ источника."""
    from app.workers.task_lock import SKIPPED_LOCKED
    _postrun({"skipped": SKIPPED_LOCKED, "task": "kg_metrics_sync"})
    assert recorder.heartbeats == []
    assert recorder.statuses == []


def test_non_allowlisted_task_is_ignored(recorder):
    _postrun(mark({"x": 1}, SourceStatus.SUCCESS), task="daily_stats_digest")
    assert recorder.heartbeats == []
    assert recorder.statuses == []


# ── обёртки задач ───────────────────────────────────────────────────────────

def test_polled_source_zero_is_success():
    """Resolve-sync без зависших алертов — источник опрошен, это SUCCESS."""
    from app.workers.tasks import _src_polled
    assert status_of(_src_polled({"stale_candidates": 0})) is SourceStatus.SUCCESS
    assert status_of(_src_polled({"error": "x"})) is SourceStatus.FAILED
    assert status_of(_src_polled({"skipped": "disabled"})) is SourceStatus.UNAVAILABLE


def test_seq_status_follows_reached_instances():
    """Прецедент 20.08.2026: rows=0 при reached=0 — это UNAVAILABLE."""
    from app.workers.tasks import _src_seq
    assert status_of(_src_seq({"instances": 8, "reached": 8, "rows": 0})) is SourceStatus.SUCCESS
    assert status_of(_src_seq({"instances": 8, "reached": 3, "rows": 10})) is SourceStatus.PARTIAL
    assert status_of(_src_seq({"instances": 8, "reached": 0, "rows": 0})) is SourceStatus.UNAVAILABLE
    assert status_of(_src_seq({"instances": 0, "reached": 0})) is SourceStatus.UNAVAILABLE
