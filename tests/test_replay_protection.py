"""Anti-replay по timestamp на подписанных вебхуках (фикс #3).

Покрытие:
- is_timestamp_fresh: свежий / старый / будущий-в-окне / далёкое будущее /
  мусор / None / отключённое окно.
- Discord _verify_signature (реальный Ed25519): свежий ts → True даже при
  валидной подписи; протухший ts → False (подпись валидна, но stale).
- AlertManager verify_alertmanager_signature: body-only (backward-compat),
  timestamp-режим fresh/stale, REQUIRE_SIGNED_TIMESTAMP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from app.api import discord_interactions, webhooks
from app.security.replay import is_timestamp_fresh


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
