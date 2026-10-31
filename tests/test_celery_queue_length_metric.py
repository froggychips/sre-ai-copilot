"""`celery_queue_length` считается по брокеру, а не выставляется никем.

До 31.10.2026 это был Gauge в app/metrics.py и функция `set_queue_length`
без единого вызова: метрика уезжала ровным нулём, а правило
`CopilotCeleryBacklog` стояло на нём с 05.09.2026 и не могло сработать.
Теперь Collector читает `LLEN` по каждой очереди на скрейпе, с лейблом
`queue` — с появлением очереди `sync` сумма скрыла бы, какая встала.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from app import metrics as m


class _FakeRedis:
    def __init__(self, lengths=None, fail=False):
        self.lengths = dict(lengths or {})
        self.fail = fail
        self.asked = []

    def llen(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.asked.append(key)
        return self.lengths.get(key, 0)


def _render(client):
    reg = CollectorRegistry()
    reg.register(m.CeleryQueueLengthCollector())
    orig = m._redis
    m._redis = lambda: client
    try:
        return generate_latest(reg).decode()
    finally:
        m._redis = orig


def test_queue_names_cover_default_sync_and_executor():
    assert m.celery_queue_names() == ("celery", "sync", "executor")


def test_one_series_per_queue_from_broker():
    fake = _FakeRedis({"celery": 3, "sync": 120})
    out = _render(fake)
    assert 'celery_queue_length{queue="celery"} 3.0' in out
    assert 'celery_queue_length{queue="sync"} 120.0' in out
    # Очередь без ключа в Redis — ноль, а не отсутствие: пустая очередь
    # и есть ноль ожидающих.
    assert 'celery_queue_length{queue="executor"} 0.0' in out
    assert fake.asked == ["celery", "sync", "executor"]


def test_no_series_when_redis_is_down():
    """Пустой ряд честнее нуля: ноль неотличим от «всё тихо»."""
    assert "celery_queue_length" not in _render(None)
    assert "celery_queue_length" not in _render(_FakeRedis(fail=True))


def test_read_queue_lengths_is_all_or_nothing():
    assert m.read_queue_lengths(_FakeRedis(fail=True), ["celery"]) is None
    assert m.read_queue_lengths(_FakeRedis({"celery": 7}), ["celery", "sync"]) == {
        "celery": 7, "sync": 0,
    }


def test_register_is_idempotent():
    reg = CollectorRegistry()
    m._registered = False
    try:
        assert m.register_queue_length_collector(reg) is True
        assert m.register_queue_length_collector(reg) is False
    finally:
        m._registered = False


def test_old_gauge_setter_is_gone():
    """Gauge, который никто не выставлял, не должен вернуться рядом с
    Collector-ом: одно имя в одном реестре — дубль серии при старте api."""
    assert not hasattr(m, "set_queue_length")
    assert not hasattr(m, "CELERY_QUEUE_LENGTH")
