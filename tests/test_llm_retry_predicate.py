"""Тесты на сужённый retry-предикат LLM-вызова (P0#3 retry-storm).

Контракт llm_retry_strategy / is_retryable_llm_error:
  - ТРАНЗИЕНТНЫЕ ошибки ретраятся: APIConnectionError, APITimeoutError,
    RateLimitError (429), APIStatusError с 5xx, asyncio.TimeoutError;
  - ДЕТЕРМИНИРОВАННЫЕ 4xx НЕ ретраятся: 400/401/403/404/422 — пробрасываются
    сразу, без повторов (не жгут токены/квоту);
  - LLMCircuitOpen не ретраится (брейкер открыт сознательно);
  - ValueError("LLM timeout") from TimeoutError — ретраибелен через __cause__.
"""
import asyncio

import anthropic
import httpx
import pytest

from app.services.resilience import (
    LLMCircuitOpen,
    is_retryable_llm_error,
    llm_retry_strategy,
)


# ── Хелперы конструирования anthropic-исключений ────────────────────────────

def _status_error(status_code: int) -> anthropic.APIStatusError:
    """APIStatusError (или его сабкласс) с нужным HTTP-статусом.

    SDK мапит статус → конкретный класс (BadRequestError и т.п.); конструируем
    через минимальный httpx.Response, как делает сам SDK.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    body = {"type": "error", "error": {"type": "x", "message": "boom"}}
    return anthropic.APIStatusError("boom", response=response, body=body)


def _connection_error() -> anthropic.APIConnectionError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(message="conn down", request=request)


def _timeout_error() -> anthropic.APITimeoutError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


def _rate_limit_error() -> anthropic.RateLimitError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    return anthropic.RateLimitError("slow", response=response, body=body)


# ── is_retryable_llm_error: РЕТРАИБЕЛЬНЫЕ ────────────────────────────────────

@pytest.mark.parametrize(
    "exc_factory",
    [
        _connection_error,
        _timeout_error,
        _rate_limit_error,
        lambda: _status_error(500),
        lambda: _status_error(503),
        lambda: _status_error(529),
        lambda: asyncio.TimeoutError(),
    ],
)
def test_retryable_errors(exc_factory):
    assert is_retryable_llm_error(exc_factory()) is True


# ── is_retryable_llm_error: НЕретраибельные (4xx + circuit) ──────────────────

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_not_retryable(status):
    assert is_retryable_llm_error(_status_error(status)) is False


def test_circuit_open_not_retryable():
    assert is_retryable_llm_error(LLMCircuitOpen("open")) is False


def test_plain_valueerror_not_retryable():
    assert is_retryable_llm_error(ValueError("nope")) is False


def test_timeout_wrapped_in_valueerror_is_retryable():
    """llm_service: raise ValueError('LLM timeout') from TimeoutError."""
    try:
        raise ValueError("LLM timeout") from asyncio.TimeoutError()
    except ValueError as e:
        assert is_retryable_llm_error(e) is True


# ── llm_retry_strategy: поведение цикла ──────────────────────────────────────

@pytest.mark.asyncio
async def test_retryable_is_retried_then_succeeds():
    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _rate_limit_error()
        return "ok"

    assert await f() == "ok"
    assert calls == 3  # 2 транзиентных сбоя + успех на 3-й


@pytest.mark.asyncio
async def test_retryable_exhausts_and_raises_last():
    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        raise _connection_error()

    with pytest.raises(anthropic.APIConnectionError):
        await f()
    assert calls == 3  # ровно retries=3 попытки


@pytest.mark.asyncio
async def test_400_not_retried_fails_fast():
    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        raise _status_error(400)

    with pytest.raises(anthropic.APIStatusError):
        await f()
    assert calls == 1  # 4xx → НИ ОДНОГО повтора


@pytest.mark.asyncio
async def test_401_not_retried_fails_fast():
    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        raise _status_error(401)

    with pytest.raises(anthropic.APIStatusError):
        await f()
    assert calls == 1


@pytest.mark.asyncio
async def test_circuit_open_fails_fast():
    calls = 0

    @llm_retry_strategy
    async def f():
        nonlocal calls
        calls += 1
        raise LLMCircuitOpen("open")

    with pytest.raises(LLMCircuitOpen):
        await f()
    assert calls == 1
