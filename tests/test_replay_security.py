"""Security-hardening для replay-ручек (fix/security-hardening).

Покрытие:
- Role-gate: неаутентифицированный → 401; аутентифицированный без роли
  "approver" → 403; с ролью → проходит (fail-closed против горизонтального
  доступа любого залогиненного юзера).
- snapshot_uri allowlist (SSRF defense-in-depth): s3:// / file:// проходят,
  http(s)/ftp/произвольный хост/мусор/пусто → ValueError. На уровне
  by-snapshot ручки чужой хост → 400.
- JWT-валидация (get_current_user): пин асимметричного алгоритма
  (HS256-downgrade → 401), обязательный iss (+ сверка при JWT_ISSUER),
  aud при JWT_AUDIENCE, пустой JWT_PUBLIC_KEY → 401, а не 500.

snapshot_uri сейчас — чистый echo (assert_replay_inputs + return), по uri
никто не ходит. Валидация добавлена как defense-in-depth на будущего
потребителя contract'а.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.api import replay
from app.auth import User, get_current_user, require_role
from app.replay.contract import (
    ALLOWED_SNAPSHOT_URI_SCHEMES,
    assert_replay_inputs,
    validate_snapshot_uri,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _make_app() -> FastAPI:
    """Мини-app с тем же монтированием replay-роутера, что в app.main:
    router-level get_current_user (→401 без токена) + per-route require_role.
    """
    app = FastAPI()
    app.include_router(
        replay.router,
        prefix="/replay",
        dependencies=[Depends(get_current_user)],
    )
    return app


def _client_as(app: FastAPI, user: User | None) -> TestClient:
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# ── role-gate ────────────────────────────────────────────────────────────


def test_replay_unauthenticated_rejected():
    """Без Authorization-заголовка router-level get_current_user → 401/403."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/replay/some-incident")
    # HTTPBearer(auto_error=True) отдаёт 403 при отсутствии заголовка,
    # 401 при битом токене — оба = «не пущен».
    assert r.status_code in (401, 403)


def test_replay_authenticated_without_role_forbidden():
    """Залогиненный viewer (нет 'approver') → 403 от require_role."""
    app = _make_app()
    user = User(sub="u1", email="u@example.com", roles=["viewer"])
    client = _client_as(app, user)
    r = client.post("/replay/some-incident")
    assert r.status_code == 403


def test_by_snapshot_authenticated_without_role_forbidden():
    app = _make_app()
    user = User(sub="u1", email="u@example.com", roles=["viewer"])
    client = _client_as(app, user)
    r = client.post("/replay/by-snapshot", params={"snapshot_id": "snap-1"})
    assert r.status_code == 403


def test_by_snapshot_with_role_valid_snapshot_id_passes():
    """approver + валидный snapshot_id → 200 accepted."""
    app = _make_app()
    user = User(sub="u1", email="u@example.com", roles=["approver"])
    client = _client_as(app, user)
    r = client.post("/replay/by-snapshot", params={"snapshot_id": "snap-1"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_by_snapshot_with_role_valid_s3_uri_passes():
    app = _make_app()
    user = User(sub="u1", email="u@example.com", roles=["approver"])
    client = _client_as(app, user)
    r = client.post(
        "/replay/by-snapshot",
        params={"snapshot_uri": "s3://snapshots/inc-42/snap.json"},
    )
    assert r.status_code == 200
    assert r.json()["snapshot_uri"] == "s3://snapshots/inc-42/snap.json"


def test_by_snapshot_with_role_malicious_host_rejected():
    """approver, но snapshot_uri на чужой http-хост → 400 (SSRF allowlist)."""
    app = _make_app()
    user = User(sub="u1", email="u@example.com", roles=["approver"])
    client = _client_as(app, user)
    r = client.post(
        "/replay/by-snapshot",
        params={"snapshot_uri": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 400


def test_require_role_unit_denies_missing_role():
    """Прямой unit на require_role: нет роли → HTTPException 403."""
    import asyncio

    from fastapi import HTTPException

    checker = require_role("approver")
    user = User(sub="u1", email="u@example.com", roles=["viewer"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(checker(user=user))
    assert exc.value.status_code == 403


# ── snapshot_uri allowlist (SSRF defense-in-depth) ───────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "s3://bucket/path/snap.json",
        "s3://bucket/snap",
        "file:///var/lib/snapshots/snap.json",
        "file://localhost/var/lib/snap.json",
        "  s3://bucket/snap.json  ",  # обрамляющие пробелы триммятся
    ],
)
def test_validate_snapshot_uri_allowed(uri):
    out = validate_snapshot_uri(uri)
    assert out == uri.strip()


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/snap.json",
        "https://example.com/snap.json",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://host/snap",
        "gopher://host/",
        "file://remote-host/etc/passwd",
        "s3://",  # нет bucket
        "//evil.com/snap",  # scheme-relative, нет схемы
        "relative/path",  # нет схемы
        "",
        "   ",
        "not a uri at all",
    ],
)
def test_validate_snapshot_uri_rejected(uri):
    with pytest.raises(ValueError):
        validate_snapshot_uri(uri)


def test_validate_snapshot_uri_too_long_rejected():
    with pytest.raises(ValueError):
        validate_snapshot_uri("s3://bucket/" + "a" * 4000)


def test_assert_replay_inputs_validates_uri():
    """assert_replay_inputs прокидывает strict-валидацию uri."""
    assert_replay_inputs(snapshot_id="snap-1")  # ok без uri
    assert_replay_inputs(snapshot_uri="s3://bucket/snap.json")  # ok валидный uri
    with pytest.raises(ValueError):
        assert_replay_inputs()  # ни id ни uri
    with pytest.raises(ValueError):
        assert_replay_inputs(snapshot_uri="http://evil.com/snap")  # чужой хост


def test_allowed_schemes_are_non_network():
    """Smoke: в allowlist нет http(s)/ftp."""
    for bad in ("http", "https", "ftp", "gopher"):
        assert bad not in ALLOWED_SNAPSHOT_URI_SCHEMES


# ── JWT-валидация: issuer / audience / algorithm pinning / fail-closed ───


@pytest.fixture(scope="module")
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


_ISSUER = "https://idp.example.com"


def _auth_settings(pub_pem: str, **overrides):
    """Двойник settings для app.auth (JWT_ISSUER добавляется централизованно;
    setattr несуществующего поля на pydantic-модели кидает ValueError)."""
    base = dict(
        JWT_PUBLIC_KEY=pub_pem,
        JWT_ALGORITHM="RS256",
        JWT_AUDIENCE=None,
        JWT_ISSUER=_ISSUER,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _claims(**overrides):
    claims = {
        "sub": "u1",
        "email": "u@example.com",
        "roles": ["approver"],
        "iss": _ISSUER,
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return claims


async def _get_user(token: str) -> User:
    return await get_current_user(SimpleNamespace(credentials=token))


@pytest.mark.asyncio
async def test_jwt_valid_rs256_with_issuer_accepted(monkeypatch, rsa_keys):
    priv, pub = rsa_keys
    monkeypatch.setattr(auth_module, "settings", _auth_settings(pub))
    token = pyjwt.encode(_claims(), priv, algorithm="RS256")
    user = await _get_user(token)
    assert user.sub == "u1"
    assert "approver" in user.roles


@pytest.mark.asyncio
async def test_jwt_missing_iss_rejected(monkeypatch, rsa_keys):
    """iss обязателен всегда — токен «того же IdP, но для другого сервиса»
    без iss не аутентифицируется."""
    priv, pub = rsa_keys
    monkeypatch.setattr(auth_module, "settings", _auth_settings(pub))
    claims = _claims()
    del claims["iss"]
    token = pyjwt.encode(claims, priv, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        await _get_user(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_wrong_issuer_rejected(monkeypatch, rsa_keys):
    priv, pub = rsa_keys
    monkeypatch.setattr(auth_module, "settings", _auth_settings(pub))
    token = pyjwt.encode(
        _claims(iss="https://other-service.example.com"), priv, algorithm="RS256"
    )
    with pytest.raises(HTTPException) as exc:
        await _get_user(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_missing_aud_rejected_when_audience_configured(monkeypatch, rsa_keys):
    priv, pub = rsa_keys
    monkeypatch.setattr(
        auth_module, "settings", _auth_settings(pub, JWT_AUDIENCE="sre-copilot")
    )
    token = pyjwt.encode(_claims(), priv, algorithm="RS256")  # без aud
    with pytest.raises(HTTPException) as exc:
        await _get_user(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_correct_audience_accepted(monkeypatch, rsa_keys):
    priv, pub = rsa_keys
    monkeypatch.setattr(
        auth_module, "settings", _auth_settings(pub, JWT_AUDIENCE="sre-copilot")
    )
    token = pyjwt.encode(_claims(aud="sre-copilot"), priv, algorithm="RS256")
    user = await _get_user(token)
    assert user.sub == "u1"


def _forge_hs256(claims: dict, secret: bytes) -> str:
    """Ручная чеканка HS256-токена: PyJWT сам отказывается encode-ить
    PEM-подобный ключ как HMAC-секрет, а атакующему это не мешает."""
    import base64
    import hashlib
    import hmac as hmac_mod
    import json

    def b64url(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    signing_input = header + b"." + payload
    sig = b64url(hmac_mod.new(secret, signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + sig).decode()


@pytest.mark.asyncio
async def test_jwt_hs256_downgrade_rejected(monkeypatch, rsa_keys):
    """JWT_ALGORITHM=HS256 (мисконфиг) не превращает ПУБЛИЧНЫЙ ключ в
    HMAC-секрет: алгоритм запинен на асимметричное семейство → 401."""
    _, pub = rsa_keys
    monkeypatch.setattr(
        auth_module, "settings", _auth_settings(pub, JWT_ALGORITHM="HS256")
    )
    # Атакующий знает публичный ключ и чеканит HS256-токен, подписанный им.
    forged = _forge_hs256(_claims(), pub.encode())
    with pytest.raises(HTTPException) as exc:
        await _get_user(forged)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_empty_public_key_returns_401_not_500(monkeypatch, rsa_keys):
    """Пустой JWT_PUBLIC_KEY: PyJWT кидает InvalidKeyError (не подкласс
    InvalidTokenError) — раньше это утекало 500-кой, теперь fail-closed 401."""
    priv, _ = rsa_keys
    monkeypatch.setattr(auth_module, "settings", _auth_settings(""))
    token = pyjwt.encode(_claims(), priv, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        await _get_user(token)
    assert exc.value.status_code == 401
