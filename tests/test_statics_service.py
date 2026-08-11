"""Тесты на statics_service: retry ЖИВОЙ, а не мёртвый код.

Находка второй волны кодревью: `@with_external_retry(retry_on=(OperationalError,
InterfaceError))` стоял поверх функций, внутри которых широкий
`except Exception → return None` перехватывал ровно эти классы ДО декоратора
(resilience.with_external_retry ретраит только на raise). Транзиентный обрыв
коннекта к statics-PG не ретраился НИ РАЗУ — при этом лог выглядел как
«statics недоступен», и вердикт молча деградировал.

Здесь проверяем контракт после фикса:
  * транзиентная ошибка → 3 попытки → None (деградация ПОСЛЕ ретраев);
  * транзиент, ушедший на 3-й попытке, даёт нормальный вердикт;
  * обрыв коннекта ПОСРЕДИ обхода таблиц ретраит проверку, а не пишет
    «query_failed» по всем таблицам;
  * детерминированные ошибки (ProgrammingError) не ретраятся;
  * каждая неудачная попытка закрывает свой коннект (psycopg2-leak).
"""
from unittest.mock import patch

import psycopg2
import pytest

from app.config import settings
from app.services.statics_service import (
    _run_latest_statics_version,
    _run_statics_check,
    get_latest_statics_version,
)

_ERROR_TEXT = "Unable to resolve IStatics CityEffectListProvider"


# ── фейковый psycopg2 ──────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._mode = None
        self._payload = None

    def execute(self, sql, params=None):
        # pgsql.SQL(...).format(...) — не str, но его repr содержит текст.
        text = sql if isinstance(sql, str) else str(sql)
        self._conn.executed.append(text)
        if "schema_migrations" in text:
            self._mode, self._payload = "all", [{"version": 10401}, {"version": 10400}]
        elif "information_schema.tables" in text:
            self._mode, self._payload = "all", [{"table_name": "effectlist_base"}]
        elif "pg_database" in text:
            self._mode, self._payload = "all", [
                {"datname": "v10401-prod", "ver": 10401},
                {"datname": "v10400-prod", "ver": 10400},
            ]
        else:  # count(*) по найденной таблице
            if self._conn.count_error is not None:
                raise self._conn.count_error
            self._mode, self._payload = "one", {"cnt": 42}

    def fetchall(self):
        return self._payload if self._mode == "all" else []

    def fetchone(self):
        return self._payload if self._mode == "one" else None


class _FakeConn:
    def __init__(self, count_error=None):
        self.autocommit = False
        self.closed_calls = 0
        self.executed: list[str] = []
        self.count_error = count_error

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def close(self):
        self.closed_calls += 1


class _Connect:
    """Фейковый psycopg2.connect: сначала N транзиентных отказов, затем conn."""

    def __init__(self, errors=(), count_error=None):
        self._errors = list(errors)
        self._count_error = count_error
        self.calls = 0
        self.conns: list[_FakeConn] = []

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        conn = _FakeConn(count_error=self._count_error)
        self.conns.append(conn)
        return conn


@pytest.fixture()
def no_sleep():
    """Backoff декоратора не должен растягивать тест на 1.5 секунды."""
    with patch("app.services.resilience.time.sleep") as s:
        yield s


# ── _run_statics_check ─────────────────────────────────────────────────────

def test_statics_check_retries_transient_then_degrades_to_none(no_sleep):
    """Обрыв коннекта = 3 попытки, и только потом None (было: 1 попытка)."""
    connect = _Connect(errors=[psycopg2.OperationalError("server closed the connection")] * 3)
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        assert _run_statics_check(_ERROR_TEXT, 5) is None
    assert connect.calls == 3
    assert no_sleep.call_count == 2  # паузы между 3 попытками


def test_statics_check_recovers_on_third_attempt(no_sleep):
    """Транзиент прошёл — вердикт нормальный, а не None."""
    connect = _Connect(errors=[
        psycopg2.OperationalError("connection reset by peer"),
        psycopg2.InterfaceError("connection already closed"),
    ])
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        out = _run_statics_check(_ERROR_TEXT, 5)
    assert connect.calls == 3
    assert out is not None
    assert "=== STATICS CHECK ===" in out
    assert "effectlist_base: 42 rows → OK" in out


def test_statics_check_transient_midloop_is_retried_not_reported_as_broken_table(no_sleep):
    """Коннект умер на count(*): это не «таблица битая» — ретраим проверку.

    Иначе вердикт наполняется query_failed по всем таблицам и уезжает в шум.
    """
    connect = _Connect(count_error=psycopg2.InterfaceError("connection already closed"))
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        out = _run_statics_check(_ERROR_TEXT, 5)
    assert out is None
    assert connect.calls == 3
    # Каждая неудачная попытка обязана закрыть свой коннект (Infra H6).
    assert [c.closed_calls for c in connect.conns] == [1, 1, 1]


def test_statics_check_deterministic_error_is_not_retried(no_sleep):
    """ProgrammingError (битый SQL / нет таблицы) — повтор бессмыслен."""
    connect = _Connect(count_error=psycopg2.ProgrammingError('relation "x" does not exist'))
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        out = _run_statics_check(_ERROR_TEXT, 5)
    assert connect.calls == 1
    assert out is not None
    assert "query_failed" in out


def test_statics_check_without_keywords_does_not_connect(no_sleep):
    connect = _Connect()
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        assert _run_statics_check("abc", 5) is None
    assert connect.calls == 0


# ── _run_latest_statics_version ────────────────────────────────────────────

def test_latest_version_retries_transient_then_degrades_to_none(no_sleep):
    connect = _Connect(errors=[psycopg2.OperationalError("timeout expired")] * 3)
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        assert _run_latest_statics_version("prod") is None
    assert connect.calls == 3
    assert no_sleep.call_count == 2


def test_latest_version_recovers_after_transient(no_sleep):
    connect = _Connect(errors=[psycopg2.OperationalError("timeout expired")])
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        out = _run_latest_statics_version("prod")
    assert connect.calls == 2
    assert out == {
        "version": 10401,
        "prev_version": 10400,
        "datname": "v10401-prod",
        "env": "prod",
    }


def test_get_latest_statics_version_requires_settings(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "STATICS_HOST", "")
    monkeypatch.setattr(settings, "STATICS_PASSWORD", "")
    connect = _Connect()
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        assert get_latest_statics_version("prod") is None
    assert connect.calls == 0


def test_get_latest_statics_version_passes_through_when_configured(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "STATICS_HOST", "statics.example")
    monkeypatch.setattr(settings, "STATICS_PASSWORD", "secret")
    connect = _Connect()
    with patch("app.services.statics_service.psycopg2.connect", new=connect):
        out = get_latest_statics_version("prod")
    assert out is not None and out["version"] == 10401
    assert connect.calls == 1
