"""Similar-past кэш (#B4): кэш должен ЧИТАТЬСЯ, а не быть write-only.

Раньше cache-key включал service_id (известен только после DB lookup) —
каждый вызов бил по БД, а Redis/local dict только пополнялись; local dict
ещё и рос неограниченно. Теперь ключ строится до запроса, есть read-path
и bounded fallback.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.discord import embed_builder as eb


@pytest.fixture(autouse=True)
def _clear_local_cache():
    eb._SIMILAR_PAST_LOCAL_CACHE.clear()
    yield
    eb._SIMILAR_PAST_LOCAL_CACHE.clear()


def _payload() -> dict:
    return {
        "alertname": "A",
        "service_name": "svc",
        "namespace": "squad-1",
        "duration_minutes": 47,
        "resolved_at": "2026-08-01T10:00:00+00:00",
        "fired_at": "2026-08-01T09:13:00+00:00",
        "resolved_by_deploy": None,
        "service_id": 7,
    }


@pytest.mark.asyncio
async def test_redis_hit_skips_db_lookup():
    """Запись в Redis есть → БД не трогаем вообще."""
    fake_redis = AsyncMock()
    fake_redis.get.return_value = json.dumps(_payload())

    with patch("app.celery_worker.redis_client", fake_redis), \
         patch.object(eb, "_lookup_similar_past_incident") as mock_db:
        out = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")

    mock_db.assert_not_called()
    assert out is not None
    assert out["duration_minutes"] == 47


@pytest.mark.asyncio
async def test_redis_negative_hit_returns_none_without_db():
    """Закэшированный negative ("{}") → None без похода в БД."""
    fake_redis = AsyncMock()
    fake_redis.get.return_value = "{}"

    with patch("app.celery_worker.redis_client", fake_redis), \
         patch.object(eb, "_lookup_similar_past_incident") as mock_db:
        out = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")

    mock_db.assert_not_called()
    assert out is None


@pytest.mark.asyncio
async def test_redis_miss_queries_db_and_writes_back():
    """Промах → DB lookup, результат пишется в Redis с TTL."""
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None

    with patch("app.celery_worker.redis_client", fake_redis), \
         patch.object(eb, "_lookup_similar_past_incident",
                      return_value=_payload()) as mock_db:
        out = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")

    mock_db.assert_called_once()
    assert out == _payload()
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.await_args
    assert args[0] == eb._similar_past_cache_key("A", "svc", "squad-1")
    assert kwargs.get("ex") == eb._SIMILAR_PAST_TTL_SEC


@pytest.mark.asyncio
async def test_negative_result_is_cached():
    """None из БД кэшируется как "{}" — не бьём БД на каждый алерт-новичок."""
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None

    with patch("app.celery_worker.redis_client", fake_redis), \
         patch.object(eb, "_lookup_similar_past_incident", return_value=None):
        out = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")

    assert out is None
    args, _ = fake_redis.set.await_args
    assert args[1] == "{}"


@pytest.mark.asyncio
async def test_redis_down_uses_local_cache_on_second_call():
    """Redis недоступен: первый вызов идёт в БД и кладёт в local, второй —
    отвечает из local без БД."""
    fake_redis = AsyncMock()
    fake_redis.get.side_effect = ConnectionError("redis down")

    with patch("app.celery_worker.redis_client", fake_redis), \
         patch.object(eb, "_lookup_similar_past_incident",
                      return_value=_payload()) as mock_db:
        out1 = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")
        out2 = await eb._lookup_similar_past_incident_cached("A", "svc", "squad-1")

    assert out1 == _payload()
    assert out2 == _payload()
    mock_db.assert_called_once()  # второй вызов — из local cache


def test_local_cache_is_bounded():
    """Fallback dict капится: старейшие вытесняются, размер ≤ MAX."""
    import time
    now = time.time()
    for i in range(eb._SIMILAR_PAST_LOCAL_CACHE_MAX + 50):
        eb._similar_past_local_put(f"k{i}", {"i": i}, now + i * 0.001)

    assert len(eb._SIMILAR_PAST_LOCAL_CACHE) <= eb._SIMILAR_PAST_LOCAL_CACHE_MAX
    # Самые свежие ключи живы, самые старые вытеснены.
    assert f"k{eb._SIMILAR_PAST_LOCAL_CACHE_MAX + 49}" in eb._SIMILAR_PAST_LOCAL_CACHE
    assert "k0" not in eb._SIMILAR_PAST_LOCAL_CACHE


def test_local_cache_ttl_expiry():
    """Протухшая запись невидима для read-path."""
    import time
    now = time.time()
    eb._similar_past_local_put("k", {"v": 1}, now - eb._SIMILAR_PAST_TTL_SEC - 10)
    hit, payload = eb._similar_past_local_get("k", now)
    assert hit is False
    assert payload is None
