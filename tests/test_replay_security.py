"""Security-hardening для replay-ручек (fix/security-hardening).

Покрытие:
- Role-gate: неаутентифицированный → 401; аутентифицированный без роли
  "approver" → 403; с ролью → проходит (fail-closed против горизонтального
  доступа любого залогиненного юзера).
- snapshot_uri allowlist (SSRF defense-in-depth): s3:// / file:// проходят,
  http(s)/ftp/произвольный хост/мусор/пусто → ValueError. На уровне
  by-snapshot ручки чужой хост → 400.

snapshot_uri сейчас — чистый echo (assert_replay_inputs + return), по uri
никто не ходит. Валидация добавлена как defense-in-depth на будущего
потребителя contract'а.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

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
