"""dedup_store: cross-replica PATCH-dedup state (PG + in-memory fallback).

PG-путь на CI недоступен (self-hosted runner без postgres) — здесь
покрывается контракт fallback-пути: семантика get_fresh/save/bump/
update_embed идентична PG-пути, чтобы PG-аварии не меняли поведение.
"""

import pytest

from app.services.discord import dedup as _dedup_state
from app.services.discord import dedup_store


@pytest.fixture(autouse=True)
def _force_memory_fallback(monkeypatch):
    """Любой PG-вызов падает → store работает на in-memory dict."""

    def _boom():
        raise RuntimeError("pg down (test)")

    monkeypatch.setattr(dedup_store, "_pg_session", _boom)
    monkeypatch.setattr(dedup_store, "_pg_warned", True)  # без warning-шума
    _dedup_state._recent_enriched.clear()
    yield
    _dedup_state._recent_enriched.clear()


def test_get_fresh_empty_returns_none():
    assert dedup_store.get_fresh("k1", ttl_sec=1800, now=1000.0) is None


def test_save_then_get_fresh_roundtrip():
    dedup_store.save(
        "k1", msg_id="m1", webhook_url="https://w", embed={"title": "t"},
        alertname="A", namespace="ns", severity="critical", now=1000.0,
    )
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=1100.0)
    assert rec is not None
    assert rec["msg_id"] == "m1"
    assert rec["count"] == 1
    assert rec["first_ts"] == 1000.0


def test_get_fresh_expired_invisible():
    dedup_store.save("k1", msg_id="m1", webhook_url="https://w", embed=None, now=1000.0)
    assert dedup_store.get_fresh("k1", ttl_sec=60, now=1100.0) is None


def test_bump_increments_atomically():
    dedup_store.save("k1", msg_id="m1", webhook_url="https://w", embed=None, now=1000.0)
    rec = dedup_store.bump("k1", now=1100.0)
    assert rec["count"] == 2
    assert rec["last_ts"] == 1100.0
    rec = dedup_store.bump("k1", now=1200.0)
    assert rec["count"] == 3


def test_bump_missing_key_returns_none():
    assert dedup_store.bump("nope", now=1000.0) is None


def test_save_overwrites_stale_with_fresh_window():
    dedup_store.save("k1", msg_id="m1", webhook_url="https://w", embed=None, now=1000.0)
    dedup_store.bump("k1", now=1100.0)
    # Новое окно: count сбрасывается, msg_id новый.
    dedup_store.save("k1", msg_id="m2", webhook_url="https://w", embed=None, now=9000.0)
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=9100.0)
    assert rec["msg_id"] == "m2"
    assert rec["count"] == 1


def test_update_embed_persists():
    dedup_store.save("k1", msg_id="m1", webhook_url="https://w", embed={"v": 1}, now=1000.0)
    dedup_store.update_embed("k1", {"v": 2})
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=1100.0)
    assert rec["embed"] == {"v": 2}
