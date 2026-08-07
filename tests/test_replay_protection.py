"""Anti-replay по timestamp на подписанных вебхуках (фикс #3).

Покрытие:
- is_timestamp_fresh: свежий / старый / будущий-в-окне / далёкое будущее /
  мусор / None / отключённое окно.
- Discord _verify_signature (реальный Ed25519): свежий ts → True даже при
  валидной подписи; протухший ts → False (подпись валидна, но stale).
- AlertManager verify_alertmanager_signature: body-only (backward-compat),
  timestamp-режим fresh/stale, REQUIRE_SIGNED_TIMESTAMP.
- Fail-closed без секрета + явный opt-out ALERTMANAGER_ALLOW_UNAUTHENTICATED.
- Seen-signature cache: повтор валидно подписанного запроса в окне → 401.
- Не-ASCII подпись → 401, а не TypeError/500.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from app.api import discord_interactions, webhooks
from app.security.replay import (
    SeenSignatureCache,
    alertmanager_signature_cache,
    is_timestamp_fresh,
)


@pytest.fixture(autouse=True)
def _clean_replay_cache():
    """Seen-signature cache — process-global; изолируем тесты друг от друга."""
    alertmanager_signature_cache.clear()
    yield
    alertmanager_signature_cache.clear()


# ── unit: is_timestamp_fresh ────────────────────────────────────────────

def test_fresh_timestamp_passes():
    now = 1_000_000.0
    assert is_timestamp_fresh(str(int(now)), 300, now=now) is True


def test_old_timestamp_fails():
    now = 1_000_000.0
    assert is_timestamp_fresh(str(int(now) - 10_000), 300, now=now) is False


def test_future_within_window_passes():
    now = 1_000_000.0
    assert is_timestamp_fresh(str(int(now) + 100), 300, now=now) is True


def test_far_future_fails():
    now = 1_000_000.0
    assert is_timestamp_fresh(str(int(now) + 10_000), 300, now=now) is False


@pytest.mark.parametrize("bad", [None, "", "not-a-number", "abc123"])
def test_garbage_timestamp_fails(bad):
    assert is_timestamp_fresh(bad, 300, now=1_000_000.0) is False


def test_disabled_window_always_true():
    assert is_timestamp_fresh("0", 0, now=1_000_000.0) is True
    assert is_timestamp_fresh(None, -1, now=1_000_000.0) is True


# ── Discord Ed25519 anti-replay ─────────────────────────────────────────

def _ed25519_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    return priv, pub_hex


def test_discord_fresh_signature_accepted():
    priv, pub_hex = _ed25519_keypair()
    ts = str(int(time.time()))
    body = b'{"type":1}'
    sig = priv.sign(ts.encode() + body).hex()
    assert discord_interactions._verify_signature(pub_hex, sig, ts, body) is True


def test_discord_stale_signature_rejected_even_if_valid():
    priv, pub_hex = _ed25519_keypair()
    ts = str(int(time.time()) - 10_000)  # 2.7ч назад — за окном 300с
    body = b'{"type":3,"data":{"custom_id":"apply_confirm_x"}}'
    sig = priv.sign(ts.encode() + body).hex()  # подпись КОРРЕКТНА
    assert discord_interactions._verify_signature(pub_hex, sig, ts, body) is False


# ── AlertManager HMAC anti-replay ───────────────────────────────────────

_SECRET = "test-secret"


def _am_request(body: bytes, signature: str, timestamp: str | None = None):
    req = MagicMock()
    headers = {"X-Alertmanager-Signature": signature}
    if timestamp is not None:
        headers["X-Alertmanager-Timestamp"] = timestamp
    req.headers = headers
    req.body = AsyncMock(return_value=body)
    return req


def _hmac(payload: bytes) -> str:
    return hmac.new(_SECRET.encode(), payload, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_am_body_only_backward_compatible(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP", False)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, _hmac(body))  # no timestamp header
    # Не должно бросить.
    assert await webhooks.verify_alertmanager_signature(req) is None


@pytest.mark.asyncio
async def test_am_timestamp_mode_fresh_accepted(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"alerts": []}).encode()
    ts = str(int(time.time()))
    req = _am_request(body, _hmac(ts.encode() + b"." + body), timestamp=ts)
    assert await webhooks.verify_alertmanager_signature(req) is None


@pytest.mark.asyncio
async def test_am_timestamp_stale_rejected(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"alerts": []}).encode()
    ts = str(int(time.time()) - 10_000)
    req = _am_request(body, _hmac(ts.encode() + b"." + body), timestamp=ts)
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(req)
    assert exc.value.status_code == 401
    assert "Stale" in exc.value.detail


@pytest.mark.asyncio
async def test_am_require_timestamp_rejects_body_only(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP", True)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, _hmac(body))  # no timestamp header
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(req)
    assert exc.value.status_code == 401
    assert "Missing AlertManager timestamp" in exc.value.detail


# ── fail-closed без секрета (фикс: auth больше не «fail-open вне production») ─


@pytest.mark.asyncio
async def test_am_no_secret_fails_closed(monkeypatch):
    """Без ALERTMANAGER_WEBHOOK_SECRET → 401 в любом ENV, а не молчаливый пропуск."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", None)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, "irrelevant")
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(req)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_am_no_secret_explicit_opt_out_allows(monkeypatch):
    """ALERTMANAGER_ALLOW_UNAUTHENTICATED=true — единственный способ жить без ключа.

    Поле добавляется в Settings централизованно; здесь подменяем модульный
    settings объектом-двойником (setattr несуществующего поля на pydantic-модели
    кидает ValueError).
    """
    fake_settings = SimpleNamespace(
        ALERTMANAGER_WEBHOOK_SECRET=None,
        ALERTMANAGER_ALLOW_UNAUTHENTICATED=True,
        ENV="development",
    )
    monkeypatch.setattr(webhooks, "settings", fake_settings)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, "irrelevant")
    assert await webhooks.verify_alertmanager_signature(req) is None


# ── seen-signature cache (anti-replay без signing-proxy) ─────────────────


@pytest.mark.asyncio
async def test_am_body_only_replay_rejected(monkeypatch):
    """Повтор того же валидно подписанного запроса в окне свежести → 401."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP", False)
    body = json.dumps({"alerts": [{"replayed": True}]}).encode()
    sig = _hmac(body)

    assert await webhooks.verify_alertmanager_signature(_am_request(body, sig)) is None
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(_am_request(body, sig))
    assert exc.value.status_code == 401
    assert "Replayed" in exc.value.detail


@pytest.mark.asyncio
async def test_am_timestamp_mode_replay_rejected(monkeypatch):
    """Timestamp-режим: окно свежести само по себе оставляло replay-дыру."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"alerts": []}).encode()
    ts = str(int(time.time()))
    sig = _hmac(ts.encode() + b"." + body)

    assert await webhooks.verify_alertmanager_signature(
        _am_request(body, sig, timestamp=ts)
    ) is None
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(_am_request(body, sig, timestamp=ts))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_am_different_bodies_not_flagged_as_replay(monkeypatch):
    """Два разных batch-а подряд — оба проходят (кэш ключуется подписью)."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body1 = json.dumps({"alerts": [1]}).encode()
    body2 = json.dumps({"alerts": [2]}).encode()
    assert await webhooks.verify_alertmanager_signature(
        _am_request(body1, _hmac(body1))
    ) is None
    assert await webhooks.verify_alertmanager_signature(
        _am_request(body2, _hmac(body2))
    ) is None


@pytest.mark.asyncio
async def test_am_replay_check_disabled_by_zero_window(monkeypatch):
    """MAX_AGE_SECONDS <= 0 — escape hatch, replay-проверка выключена."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(
        webhooks.settings, "ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS", 0
    )
    body = json.dumps({"alerts": []}).encode()
    sig = _hmac(body)
    assert await webhooks.verify_alertmanager_signature(_am_request(body, sig)) is None
    assert await webhooks.verify_alertmanager_signature(_am_request(body, sig)) is None


def test_seen_signature_cache_ttl_and_eviction():
    """Unit на кэш: TTL-протухание и bounded-eviction старейших записей."""
    cache = SeenSignatureCache(max_entries=2)
    now = 1_000_000.0
    assert cache.seen_recently("a", 300, now=now) is False
    assert cache.seen_recently("a", 300, now=now + 1) is True  # replay в окне
    assert cache.seen_recently("a", 300, now=now + 301) is False  # протух

    cache.clear()
    assert cache.seen_recently("s1", 300, now=now) is False
    assert cache.seen_recently("s2", 300, now=now) is False
    assert cache.seen_recently("s3", 300, now=now) is False  # вытесняет s1
    assert cache.seen_recently("s1", 300, now=now + 1) is False  # s1 забыт (bounded)
    assert cache.seen_recently("s3", 300, now=now + 1) is True


# ── не-ASCII подпись: 401, а не TypeError → 500 ──────────────────────────


@pytest.mark.asyncio
async def test_am_non_ascii_signature_rejected_401(monkeypatch):
    """Starlette декодирует заголовки latin-1; 0xFF байт ронял compare_digest."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, "\xff" * 16)
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(req)
    assert exc.value.status_code == 401
