"""Тесты на with_external_retry decorator (Grok review #4).

Контракт:
  - async и sync функции одинаково поддержаны (detected через iscoroutinefunction)
  - exponential backoff (delay × factor каждую следующую попытку)
  - retry_on фильтрует exception types
  - max_attempts=1 → effectively no retry
  - финальный exception пробрасывается
"""
from unittest.mock import patch

import pytest

from app.services.resilience import with_external_retry


# ── Async branch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_async_returns_immediately_on_success():
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        return 42

    assert await f() == 42
    assert calls == 1


@pytest.mark.asyncio
async def test_async_retries_until_success():
    """3 attempts, first 2 throw — 3-я возвращает значение."""
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("transient")
        return "ok"

    assert await f() == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_async_raises_last_exception_when_attempts_exhausted():
    calls = 0

    @with_external_retry(max_attempts=2, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        raise TimeoutError(f"attempt-{calls}")

    with pytest.raises(TimeoutError, match="attempt-2"):
        await f()
    assert calls == 2


@pytest.mark.asyncio
async def test_async_retry_on_filter_lets_other_exceptions_through():
    """retry_on=(ConnectionError,) → ValueError НЕ retry-ится, бросается сразу."""
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0, retry_on=(ConnectionError,))
    async def f():
        nonlocal calls
        calls += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        await f()
    assert calls == 1  # один call, retry не сработал


@pytest.mark.asyncio
async def test_async_exponential_backoff_delays():
    """delay: 0.1, 0.2, 0.4 (factor=2.0)."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    @with_external_retry(max_attempts=4, initial_delay=0.1, backoff_factor=2.0)
    async def f():
        raise ConnectionError("x")

    with patch("app.services.resilience.asyncio.sleep", new=fake_sleep):
        with pytest.raises(ConnectionError):
            await f()

    # 4 attempts, 3 sleeps между ними
    assert sleeps == [0.1, 0.2, 0.4]


# ── Sync branch ────────────────────────────────────────────────────────────

def test_sync_returns_immediately_on_success():
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    def f():
        nonlocal calls
        calls += 1
        return "ok"

    assert f() == "ok"
    assert calls == 1


def test_sync_retries_until_success():
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    def f():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise IOError("transient")
        return calls

    assert f() == 3


def test_sync_raises_after_exhaustion():
    @with_external_retry(max_attempts=2, initial_delay=0)
    def f():
        raise OSError("never works")

    with pytest.raises(OSError, match="never works"):
        f()


def test_sync_exponential_backoff_uses_time_sleep():
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    @with_external_retry(max_attempts=3, initial_delay=0.5, backoff_factor=3.0)
    def f():
        raise ConnectionError("x")

    with patch("app.services.resilience.time.sleep", new=fake_sleep):
        with pytest.raises(ConnectionError):
            f()

    # 3 attempts → 2 sleeps: 0.5, 1.5
    assert sleeps == [0.5, 1.5]


# ── max_attempts=1 = no-retry sanity ───────────────────────────────────────

@pytest.mark.asyncio
async def test_max_attempts_one_is_no_retry():
    calls = 0

    @with_external_retry(max_attempts=1, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        raise RuntimeError("once")

    with pytest.raises(RuntimeError):
        await f()
    assert calls == 1


# ── Decorator preserves function metadata ──────────────────────────────────

def test_decorator_preserves_function_name():
    @with_external_retry(max_attempts=2, initial_delay=0)
    async def my_named_function():
        return None

    assert my_named_function.__name__ == "my_named_function"


# ── Applied to real services (smoke grep) ──────────────────────────────────

def test_jira_search_by_service_is_retry_wrapped():
    """Smoke: ensure decorator applied to integration entry points."""
    src = open("app/context/jira_client.py").read()
    assert "@with_external_retry" in src
    assert "jira.search_by_service" in src


def test_vm_methods_are_retry_wrapped():
    src = open("app/context/vm_client.py").read()
    assert src.count("@with_external_retry") >= 2  # get_cluster_health + query_range


def test_clickhouse_query_is_retry_wrapped():
    src = open("app/services/clickhouse_service.py").read()
    assert "@with_external_retry" in src


def test_statics_check_is_retry_wrapped():
    src = open("app/services/statics_service.py").read()
    assert "@with_external_retry" in src
    # statics retry-ится только на connection-class ошибках
    assert "OperationalError" in src or "psycopg2.OperationalError" in src
