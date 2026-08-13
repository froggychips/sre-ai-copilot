"""Межпрогонное состояние дайджеста: снапшоты и heartbeat.

Главное свойство слоя — **fail-open**: Redis здесь канал наблюдаемости, и его
недоступность обязана деградировать в «вчерашних данных нет» (дайджест
напишет «new baseline»), а не ронять сборку или beat-таск. Проверяется именно
это, а не то, что redis-py умеет писать ключи.
"""
from datetime import datetime, timezone

import pytest

from app.services.digest import state


class FakeAsyncRedis:
    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.fail = fail
        self.written = {}

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis недоступен")
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis недоступен")
        self.written[key] = (value, ex)


@pytest.fixture
def fake_async(monkeypatch):
    def _install(values=None, fail=False):
        client = FakeAsyncRedis(values, fail)

        async def _client():
            return client

        monkeypatch.setattr(state, "_async_client", _client)
        return client
    return _install


# --- снапшоты -------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_snapshot_reads_as_none(fake_async):
    fake_async({})
    assert await state.read_day_snapshot() is None


@pytest.mark.asyncio
async def test_dead_redis_reads_as_none_not_raise(fake_async):
    """Redis лежит — дайджест обязан собраться и сказать «new baseline»."""
    fake_async(fail=True)
    assert await state.read_day_snapshot() is None
    assert await state.read_topology_snapshot() is None
    assert await state.read_last_firing_series() is None


@pytest.mark.asyncio
async def test_dead_redis_write_does_not_raise(fake_async):
    fake_async(fail=True)
    await state.write_day_snapshot({"nodes_ready": 12})   # не бросает
    await state.write_last_firing_series(673)
    await state.write_topology_snapshot({"services": 400})


@pytest.mark.asyncio
async def test_snapshot_roundtrip_carries_ttl(fake_async):
    client = fake_async({})
    await state.write_day_snapshot({"nodes_ready": 12, "crashloops": 3})
    value, ex = client.written[state.DAY_SNAPSHOT_REDIS_KEY]
    assert ex == state.DAY_SNAPSHOT_REDIS_TTL == 25 * 3600
    assert '"nodes_ready": 12' in value


@pytest.mark.asyncio
async def test_broken_json_reads_as_none(fake_async):
    """Битый снапшот не должен ронять сборку — это те же «нет данных»."""
    fake_async({state.DAY_SNAPSHOT_REDIS_KEY: "{не json"})
    assert await state.read_day_snapshot() is None


@pytest.mark.asyncio
async def test_firing_series_parsed_as_int(fake_async):
    fake_async({state.FIRING_SERIES_REDIS_KEY: "626"})
    assert await state.read_last_firing_series() == 626


@pytest.mark.parametrize("snapshot,expected", [
    (None, True), ({}, True), ({"nodes_ready": 12}, False),
])
def test_detect_first_run(snapshot, expected):
    assert state.detect_first_run(snapshot) is expected


# --- heartbeat ------------------------------------------------------------


class FakeSyncRedis:
    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.fail = fail
        self.written = {}

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis недоступен")
        return self.values.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis недоступен")
        self.written[key] = (value, ex)


def test_heartbeat_key_is_prefixed():
    assert state.beat_heartbeat_key("kg_metrics_sync") == "stats:beat:last_run:kg_metrics_sync"


def test_heartbeat_write_stores_iso_with_ttl(monkeypatch):
    client = FakeSyncRedis()
    monkeypatch.setattr(state, "_get_beat_redis", lambda: client)
    ts = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    state.record_task_heartbeat("kg_metrics_sync", ts)
    value, ex = client.written["stats:beat:last_run:kg_metrics_sync"]
    assert value == ts.isoformat()
    assert ex == state.BEAT_HEARTBEAT_REDIS_TTL == 7 * 24 * 3600


def test_heartbeat_write_survives_dead_redis(monkeypatch):
    """Beat-таск не должен падать из-за monitoring-канала."""
    monkeypatch.setattr(state, "_get_beat_redis", lambda: FakeSyncRedis(fail=True))
    state.record_task_heartbeat("kg_metrics_sync")  # не бросает


def test_heartbeat_read_returns_datetime(monkeypatch):
    ts = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    client = FakeSyncRedis({"stats:beat:last_run:kg_topology_sync": ts.isoformat()})
    monkeypatch.setattr(state, "_get_beat_redis", lambda: client)
    assert state.get_beat_last_run("kg_topology_sync") == ts


def test_heartbeat_read_without_key_is_none(monkeypatch):
    """Таск ещё ни разу не отработал после передеплоя — это не ошибка."""
    monkeypatch.setattr(state, "_get_beat_redis", lambda: FakeSyncRedis({}))
    assert state.get_beat_last_run("kg_topology_sync") is None


def test_heartbeat_read_survives_dead_redis(monkeypatch):
    monkeypatch.setattr(state, "_get_beat_redis", lambda: FakeSyncRedis(fail=True))
    assert state.get_beat_last_run("kg_topology_sync") is None


def test_sync_client_is_reused_within_same_redis_module(monkeypatch):
    """Клиент кэшируется: heartbeat пишется каждым beat-таском, и построение
    нового redis.Redis на каждый вызов давало постоянный connection churn."""
    import sys
    import types

    created = []

    class FakeRedisModule(types.ModuleType):
        class Redis:
            @staticmethod
            def from_url(url, decode_responses=False):
                created.append(url)
                return FakeSyncRedis()

    monkeypatch.setattr(state, "_beat_redis_cache", None)
    monkeypatch.setitem(sys.modules, "redis", FakeRedisModule("redis"))
    first = state._get_beat_redis()
    second = state._get_beat_redis()
    assert first is second
    assert len(created) == 1
