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
- Общий (Redis) nonce-store: подпись одноразова для ВСЕХ реплик; недоступность
  store'а НЕ расширяет окно replay; cooldown после ошибки; фактическое
  остаточное поведение body-only подписи после забытого окна.
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
from app.security import replay as replay_module
from app.security.replay import (
    RedisNonceStore,
    SeenSignatureCache,
    alertmanager_signature_cache,
    is_timestamp_fresh,
    shared_nonce_enabled,
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


# ── общий (Redis) nonce-store: replay между репликами ────────────────────


class FakeNonceStore:
    """In-memory двойник общего store'а: SET NX EX без сети.

    `unavailable=True` эмулирует лежащий Redis (claim → None).
    """

    def __init__(self) -> None:
        self.keys: dict[str, float] = {}  # key → момент протухания
        self.unavailable = False
        self.claims = 0

    def claim(self, key: str, ttl_seconds: int, now=None):
        self.claims += 1
        if self.unavailable:
            return None
        current = time.time() if now is None else now
        expires = self.keys.get(key)
        if expires is not None and expires > current:
            return False
        self.keys[key] = current + ttl_seconds
        return True


def _two_replicas(store):
    """Две независимые api-реплики (свой process-cache) с ОДНИМ store'ом."""
    return (
        SeenSignatureCache(shared_store=store),
        SeenSignatureCache(shared_store=store),
    )


def test_shared_store_closes_cross_replica_replay():
    """Перехваченный вебхук больше не проигрывается «по разу на реплику»."""
    store = FakeNonceStore()
    replica_a, replica_b = _two_replicas(store)
    now = 1_000_000.0

    assert replica_a.seen_recently("sig", 300, now=now) is False  # принят
    # Вторая реплика своего локального окна не имеет, но общий ключ уже занят.
    assert replica_b.seen_recently("sig", 300, now=now + 1) is True


def test_shared_store_unavailable_does_not_widen_window():
    """Redis лёг → защита падает до per-process уровня, но не до fail-open.

    Внутри одной реплики повтор всё равно 401 (локальное окно), а «дыра»
    ровно та, что была ДО общего store'а: по одному разу на реплику.
    """
    store = FakeNonceStore()
    store.unavailable = True
    replica_a, replica_b = _two_replicas(store)
    now = 1_000_000.0

    assert replica_a.seen_recently("sig", 300, now=now) is False
    assert replica_a.seen_recently("sig", 300, now=now + 1) is True  # локально
    # Фактическое (не идеальное) поведение при недоступном store: вторая
    # реплика примет тот же запрос один раз. Шире прежнего окно не стало.
    assert replica_b.seen_recently("sig", 300, now=now + 1) is False


def test_local_hit_short_circuits_shared_store():
    """Локальный хит не ходит в Redis: дешёвая проверка первая."""
    store = FakeNonceStore()
    cache = SeenSignatureCache(shared_store=store)
    now = 1_000_000.0

    assert cache.seen_recently("sig", 300, now=now) is False
    assert store.claims == 1
    assert cache.seen_recently("sig", 300, now=now + 1) is True
    assert store.claims == 1  # второй раз в store не пошли


def test_shared_store_ttl_expiry_forgets_signature():
    """Ключ живёт ровно окно свежести — после него подпись снова принимается."""
    store = FakeNonceStore()
    replica_a, replica_b = _two_replicas(store)
    now = 1_000_000.0

    assert replica_a.seen_recently("sig", 300, now=now) is False
    assert replica_b.seen_recently("sig", 300, now=now + 301) is False


def test_disabled_window_skips_shared_store():
    """ttl <= 0 — escape hatch: ни локально, ни в Redis ничего не пишем."""
    store = FakeNonceStore()
    cache = SeenSignatureCache(shared_store=store)
    assert cache.seen_recently("sig", 0, now=1_000_000.0) is False
    assert store.claims == 0


def test_clear_does_not_touch_shared_store():
    """clear() — только локальный кэш; чужие реплики не «прощают» подпись."""
    store = FakeNonceStore()
    cache = SeenSignatureCache(shared_store=store)
    now = 1_000_000.0
    assert cache.seen_recently("sig", 300, now=now) is False
    cache.clear()
    assert cache.seen_recently("sig", 300, now=now + 1) is True  # ключ в store


# ── включение общего store'а ──────────────────────────────────────────────


def _fake_settings(monkeypatch, *, is_production: bool, configured=None) -> None:
    """Подменяет app.config.settings: shared_nonce_enabled импортирует его
    ЛЕНИВО, внутри вызова, поэтому подмена модульного атрибута работает.
    """
    import app.config as config_module

    monkeypatch.setattr(
        config_module,
        "settings",
        SimpleNamespace(
            is_production=is_production,
            ALERTMANAGER_REPLAY_SHARED_NONCE=configured,
            REDIS_URL="redis://localhost:6379/0",
        ),
    )


def test_shared_nonce_enabled_defaults_to_production(monkeypatch):
    """Дефолт — только прод: реплик там >1, а тесты не должны зависеть от Redis."""
    monkeypatch.delenv("ALERTMANAGER_REPLAY_SHARED_NONCE", raising=False)
    _fake_settings(monkeypatch, is_production=True)
    assert shared_nonce_enabled() is True
    _fake_settings(monkeypatch, is_production=False)
    assert shared_nonce_enabled() is False


@pytest.mark.parametrize(
    "raw,expected", [("true", True), ("1", True), ("off", False), ("false", False)]
)
def test_shared_nonce_env_override(monkeypatch, raw, expected):
    """Env-переменная перебивает дефолт по ENV в обе стороны."""
    _fake_settings(monkeypatch, is_production=False)
    monkeypatch.setenv("ALERTMANAGER_REPLAY_SHARED_NONCE", raw)
    assert shared_nonce_enabled() is expected


def test_shared_nonce_settings_field_wins_over_env(monkeypatch):
    """Явное поле настроек сильнее env-переменной (если его добавят в config)."""
    _fake_settings(monkeypatch, is_production=True, configured=False)
    monkeypatch.setenv("ALERTMANAGER_REPLAY_SHARED_NONCE", "true")
    assert shared_nonce_enabled() is False


def test_default_cache_uses_shared_store_when_enabled(monkeypatch):
    """Продовый инстанс кэша реально резолвит общий store (а не игнорирует флаг)."""
    store = FakeNonceStore()
    monkeypatch.setattr(replay_module, "_default_nonce_store", lambda: store)
    now = 1_000_000.0
    assert alertmanager_signature_cache.seen_recently("sig", 300, now=now) is False
    assert store.claims == 1


# ── RedisNonceStore: деградация без сети ──────────────────────────────────


def test_redis_nonce_store_maps_setnx_result(monkeypatch):
    """redis-py: True → зарегистрировали, None (NX не сработал) → replay."""
    store = RedisNonceStore("redis://localhost:6379/0")
    client = SimpleNamespace(calls=[])

    def _set(key, value, nx=False, ex=None):
        client.calls.append((key, value, nx, ex))
        return True if len(client.calls) == 1 else None

    client.set = _set
    monkeypatch.setattr(store, "_connect", lambda: client)

    assert store.claim("sig", 300) is True
    assert store.claim("sig", 300) is False
    key, value, nx, ex = client.calls[0]
    assert key.endswith("sig") and nx is True and ex == 300


def test_redis_nonce_store_cooldown_after_failure():
    """После ошибки store молчит `cooldown_seconds` — не платим таймаут каждый раз."""
    attempts = {"n": 0}

    store = RedisNonceStore("redis://localhost:6379/0", cooldown_seconds=30.0)

    def _boom():
        attempts["n"] += 1
        raise ConnectionError("redis unavailable")

    store._connect = _boom  # type: ignore[method-assign]
    now = 1_000_000.0

    assert store.claim("sig", 300, now=now) is None
    assert store.claim("sig", 300, now=now + 1) is None
    assert attempts["n"] == 1  # в cooldown к сети не ходим
    assert store.claim("sig", 300, now=now + 31) is None
    assert attempts["n"] == 2  # cooldown вышел — пробуем снова


def test_redis_nonce_store_reconnects_after_command_error(monkeypatch):
    """Ошибка самой команды тоже роняет клиента в cooldown, а не зависает."""
    store = RedisNonceStore("redis://localhost:6379/0", cooldown_seconds=5.0)

    def _client():
        c = SimpleNamespace()

        def _set(*_a, **_kw):
            raise TimeoutError("timed out")

        c.set = _set
        return c

    monkeypatch.setattr(store, "_connect", _client)
    now = 1_000_000.0
    assert store.claim("sig", 300, now=now) is None
    assert store._client is None  # клиент выброшен, следующий раз — новый


def test_real_store_against_dead_redis_degrades_to_local_window():
    """Включённый общий store + мёртвый Redis = поведение как до его появления.

    Живой сети тут нет: клиент смотрит на заведомо закрытый порт. Проверяем, что
    (а) ошибка не всплывает наружу (иначе вебхук отвечал бы 500 вместо приёма),
    (б) локальное окно всё ещё режет повтор,
    (в) новые подписи продолжают проходить (не fail-closed по всему трафику).
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    store = RedisNonceStore(
        f"redis://127.0.0.1:{dead_port}/0",
        timeout_seconds=0.05,
        cooldown_seconds=30.0,
    )
    cache = SeenSignatureCache(shared_store=store)
    now = 1_000_000.0

    assert cache.seen_recently("sig", 300, now=now) is False
    assert cache.seen_recently("sig", 300, now=now + 1) is True
    assert cache.seen_recently("other-sig", 300, now=now + 2) is False


# ── остаточный риск body-only подписи (зафиксировано как есть) ────────────


@pytest.mark.asyncio
async def test_am_body_only_replay_passes_after_window_forgotten(monkeypatch):
    """ФАКТИЧЕСКОЕ поведение: body-only подпись сама по себе НЕ протухает.

    Забытое окно (рестарт реплики / протухший nonce-ключ) — и тот же
    перехваченный batch снова валиден: HMAC считается только по телу, в нём
    нет ничего, что стареет. Единственное лечение — signed-timestamp режим
    (см. следующий тест), поэтому он и объявлен предпочтительным в SECURITY.md.
    """
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(
        webhooks.settings, "ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP", False
    )
    body = json.dumps({"alerts": [{"captured": True}]}).encode()
    sig = _hmac(body)

    assert await webhooks.verify_alertmanager_signature(_am_request(body, sig)) is None
    # Окно забыто (рестарт процесса / истёкший TTL ключа).
    alertmanager_signature_cache.clear()
    assert await webhooks.verify_alertmanager_signature(_am_request(body, sig)) is None


@pytest.mark.asyncio
async def test_am_signed_timestamp_expires_captured_request(monkeypatch):
    """Signed-timestamp режим закрывает то, что nonce-кэш закрыть не может.

    Тот же перехваченный запрос после окна отклоняется даже с ПУСТЫМ кэшем:
    свежесть зашита в саму подпись.
    """
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(
        webhooks.settings, "ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP", True
    )
    body = json.dumps({"alerts": [{"captured": True}]}).encode()
    ts = str(int(time.time()) - 10_000)  # запрос был подписан давно
    sig = _hmac(ts.encode() + b"." + body)

    alertmanager_signature_cache.clear()
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(
            _am_request(body, sig, timestamp=ts)
        )
    assert exc.value.status_code == 401
    assert "Stale" in exc.value.detail


@pytest.mark.asyncio
async def test_am_non_ascii_signature_rejected_401(monkeypatch):
    """Starlette декодирует заголовки latin-1; 0xFF байт ронял compare_digest."""
    monkeypatch.setattr(webhooks.settings, "ALERTMANAGER_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"alerts": []}).encode()
    req = _am_request(body, "\xff" * 16)
    with pytest.raises(HTTPException) as exc:
        await webhooks.verify_alertmanager_signature(req)
    assert exc.value.status_code == 401
