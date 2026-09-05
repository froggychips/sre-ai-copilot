"""Результат self-health виден снаружи, а не только в audit-логе.

Проверки умеют находить ровно те поломки, ради которых их писали, — но за
05.09.2026 ни одна из трёх найденных (мёртвый пятнадцать суток источник
statics, 96 недоставок алертов в сутки, чистка графа, заблокированная
собственным порогом) не всплыла сама. Всплывать было негде: метрик у
копилота в VictoriaMetrics не было вообще, включая `celery_queue_length`, на
которую ссылалось единственное написанное правило.
"""
from __future__ import annotations

import json

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from app.knowledge_graph import self_health_metrics as shm


class _FakeRedis:
    """Минимум, который использует экспортёр: set/get с ex."""

    def __init__(self, initial=None, fail=False):
        self.store = dict(initial or {})
        self.fail = fail
        self.last_ex = None

    def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value
        self.last_ex = ex

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)


class _Result:
    def __init__(self, name, status):
        self.name = name
        self.status = status


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(shm, "_redis", lambda: fake)
    return fake


def _render(registry):
    return generate_latest(registry).decode()


def _registry_with_collector():
    reg = CollectorRegistry()
    reg.register(shm.SelfHealthCollector())
    return reg


def test_snapshot_round_trip(redis):
    ok = shm.publish_snapshot("warn", [_Result("sync_lag", "warn")], 1757000000.0)

    assert ok is True
    assert redis.last_ex == shm.SNAPSHOT_TTL_SECONDS
    snap = shm.read_snapshot()
    assert snap["overall"] == "warn"
    assert snap["checks"] == [{"name": "sync_lag", "status": "warn"}]


def test_statuses_are_numbers_not_labels(redis):
    """`>= 2` в правиле проще и надёжнее сравнения строкового лейбла."""
    shm.publish_snapshot(
        "fail",
        [_Result("sync_lag", "ok"), _Result("digest_delivery", "warn"),
         _Result("graph_integrity", "fail")],
        1757000000.0,
    )

    text = _render(_registry_with_collector())

    assert 'copilot_self_health_check_status{check="sync_lag"} 0.0' in text
    assert 'copilot_self_health_check_status{check="digest_delivery"} 1.0' in text
    assert 'copilot_self_health_check_status{check="graph_integrity"} 2.0' in text
    assert "copilot_self_health_status 2.0" in text
    assert "copilot_self_health_last_run_timestamp 1.757e+09" in text


def test_missing_snapshot_yields_nothing(redis):
    """Снимка нет — молчим.

    Ноль здесь означает «проверено, всё хорошо». Выдавать его за «не знаю» —
    ровно та подмена, из-за которой мёртвый источник statics пятнадцать суток
    считался здоровым.
    """
    text = _render(_registry_with_collector())

    assert "copilot_self_health" not in text


def test_broken_redis_does_not_break_scrape(monkeypatch):
    """Экспортёр не имеет права уронить /metrics вместе с собой."""
    monkeypatch.setattr(shm, "_redis", lambda: _FakeRedis(fail=True))

    text = _render(_registry_with_collector())

    assert "copilot_self_health" not in text


def test_malformed_snapshot_is_ignored(monkeypatch):
    monkeypatch.setattr(
        shm, "_redis",
        lambda: _FakeRedis({shm.SNAPSHOT_KEY: "{не json"}),
    )

    assert shm.read_snapshot() is None
    assert "copilot_self_health" not in _render(_registry_with_collector())


def test_unknown_status_is_not_silently_ok(monkeypatch):
    """Незнакомый статус — 3, а не 0: молчаливое `ok` здесь хуже шума."""
    monkeypatch.setattr(shm, "_redis", lambda: _FakeRedis({
        shm.SNAPSHOT_KEY: json.dumps({
            "overall": "weird",
            "ts": 1757000000.0,
            "checks": [{"name": "sync_lag", "status": "weird"}],
        }),
    }))

    text = _render(_registry_with_collector())

    assert 'copilot_self_health_check_status{check="sync_lag"} 3.0' in text
    assert "copilot_self_health_status 3.0" in text


def test_publish_failure_is_reported_not_raised(monkeypatch):
    """Ошибка записи метрик не должна валить прогон проверок."""
    monkeypatch.setattr(shm, "_redis", lambda: _FakeRedis(fail=True))

    assert shm.publish_snapshot("ok", [_Result("sync_lag", "ok")], 1.0) is False


def test_register_collector_is_idempotent():
    """Повторная регистрация в prometheus_client — исключение, а не no-op."""
    reg = CollectorRegistry()
    shm._registered = False
    try:
        assert shm.register_collector(reg) is True
        assert shm.register_collector(reg) is False
    finally:
        shm._registered = False
