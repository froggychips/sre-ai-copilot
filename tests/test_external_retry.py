"""Тесты на with_external_retry decorator (Grok review #4).

Контракт:
  - async и sync функции одинаково поддержаны (detected через iscoroutinefunction)
  - exponential backoff (delay × factor каждую следующую попытку)
  - retry_on фильтрует exception types
  - дефолтный retry_on сужен до транзиентных (OSError + httpx.TransportError):
    HTTP 4xx/SQL-ошибки детерминированы и НЕ ретраятся
  - max_attempts=1 → effectively no retry
  - финальный exception пробрасывается

Плюс llm_retry_strategy: jitter в backoff-е и уважение Retry-After на 429.
"""
from unittest.mock import patch

import httpx
import pytest

from app.services.resilience import llm_retry_strategy, with_external_retry


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


# ── Narrowed default retry_on ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_does_not_retry_http_status_error():
    """Дефолт НЕ ретраит httpx.HTTPStatusError: 401/400/битый SQL от
    ClickHouse детерминированы — прежний дефолт (Exception,) гонял их 3×."""
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "http://ch:8123/")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("auth", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        await f()
    assert calls == 1  # ни одного повтора


@pytest.mark.asyncio
async def test_default_retries_httpx_transport_error():
    """Транспортные ошибки httpx (таймаут коннекта и т.п.) — транзиент, ретраим."""
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise httpx.ConnectTimeout("slow")
        return "ok"

    assert await f() == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_default_does_not_retry_value_error():
    """Программные ошибки (ValueError и т.п.) дефолт не ретраит."""
    calls = 0

    @with_external_retry(max_attempts=3, initial_delay=0)
    async def f():
        nonlocal calls
        calls += 1
        raise ValueError("bug, not transient")

    with pytest.raises(ValueError):
        await f()
    assert calls == 1


# ── llm_retry_strategy: jitter + Retry-After ───────────────────────────────

def _rate_limit_error(retry_after: str | None = None):
    import anthropic

    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, headers=headers)
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    return anthropic.RateLimitError("slow", response=response, body=body)


@pytest.mark.asyncio
async def test_llm_retry_honors_retry_after_on_429():
    """429 с Retry-After → ждём срок провайдера (+jitter ≤0.5s), а не свой backoff."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _rate_limit_error(retry_after="7")
        return "ok"

    with patch("app.services.resilience.asyncio.sleep", new=fake_sleep):
        assert await f() == "ok"

    assert len(sleeps) == 1
    assert 7.0 <= sleeps[0] <= 7.5  # Retry-After + jitter до 0.5s


@pytest.mark.asyncio
async def test_llm_retry_backoff_is_jittered():
    """Без Retry-After — линейная база × jitter ±50% (разлепляет волны fan-out-а)."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    @llm_retry_strategy
    async def f():
        raise _rate_limit_error()  # без Retry-After

    with patch("app.services.resilience.asyncio.sleep", new=fake_sleep):
        with pytest.raises(Exception):
            await f()

    # retries=3 → 2 сна; base=0.5: attempt1 ∈ [0.25, 0.75], attempt2 ∈ [0.5, 1.5]
    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.75
    assert 0.5 <= sleeps[1] <= 1.5


@pytest.mark.asyncio
async def test_llm_retry_caps_malicious_retry_after():
    """Злой/огромный Retry-After зажимается капом — worker не виснет на минуты."""
    from app.services.resilience import _RETRY_AFTER_CAP_SECONDS

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    @llm_retry_strategy
    async def f():
        raise _rate_limit_error(retry_after="86400")

    with patch("app.services.resilience.asyncio.sleep", new=fake_sleep):
        with pytest.raises(Exception):
            await f()

    assert all(s <= _RETRY_AFTER_CAP_SECONDS + 0.5 for s in sleeps)


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
