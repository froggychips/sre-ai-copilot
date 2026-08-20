"""Окно подавления повторов, общее для всех форков воркера.

До этого каждая задача держала «уже сообщали» в словаре модуля. В
prefork-воркере такой словарь принадлежит одному форку: при
`--concurrency=4` это четыре независимых копии, а recycle по
`worker_max_memory_per_child` обнуляет их постоянно — шестичасовое окно на
практике держалось десятки минут.
"""
import pytest

from app.core import fire_dedup


class FakeRedis:
    """Словарь вместо сети. TTL не эмулируем — истечение проверяем удалением."""

    def __init__(self, store):
        self.store = store

    def exists(self, k):
        return 1 if k in self.store else 0

    def setex(self, k, ttl, v):
        self.store[k] = (v, ttl)

    def delete(self, k):
        self.store.pop(k, None)

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        return iter([k for k in list(self.store) if k.startswith(prefix)])


@pytest.fixture
def store(monkeypatch):
    s: dict = {}
    monkeypatch.setattr(fire_dedup, "_redis", lambda: FakeRedis(s))
    return s


def test_first_fire_allowed_then_suppressed(store):
    assert fire_dedup.should_fire("self_health", "abc") is True
    fire_dedup.mark_fired("self_health", "abc", 3600)
    assert fire_dedup.should_fire("self_health", "abc") is False


def test_suppression_survives_a_fork_restart(store):
    """То, ради чего состояние вынесено из памяти процесса.

    Перезагрузка модуля имитирует новый форк: раньше словарь уезжал вместе с
    процессом, и следующий форк отправлял то же сообщение заново.
    """
    import importlib

    fire_dedup.mark_fired("self_health", "abc", 3600)
    reloaded = importlib.reload(fire_dedup)
    reloaded._redis = lambda: FakeRedis(store)   # новый форк, тот же Redis
    assert reloaded.should_fire("self_health", "abc") is False


def test_channels_do_not_shadow_each_other(store):
    """Одинаковый fingerprint у разных задач не должен глушить друг друга."""
    fire_dedup.mark_fired("self_health", "same", 3600)
    assert fire_dedup.should_fire("stuck_alerts", "same") is True


def test_different_fingerprints_are_independent(store):
    fire_dedup.mark_fired("self_health", "one", 3600)
    assert fire_dedup.should_fire("self_health", "two") is True


def test_clear_one_fingerprint(store):
    fire_dedup.mark_fired("self_health", "abc", 3600)
    fire_dedup.clear("self_health", "abc")
    assert fire_dedup.should_fire("self_health", "abc") is True


def test_clear_whole_channel(store):
    """Состояние починили — ждать конца окна незачем."""
    for fp in ("a", "b", "c"):
        fire_dedup.mark_fired("self_health", fp, 3600)
    fire_dedup.mark_fired("stuck_alerts", "keep", 3600)
    fire_dedup.clear("self_health")
    assert all(fire_dedup.should_fire("self_health", fp) for fp in ("a", "b", "c"))
    assert fire_dedup.should_fire("stuck_alerts", "keep") is False, "снесён чужой канал"


def test_window_is_passed_as_ttl(store):
    """Окно живёт как TTL ключа, а не как сравнение таймстемпов.

    Так истечение — забота Redis, и ключи не копятся: набор fingerprint'ов
    со временем меняется, без TTL они оставались бы навсегда.
    """
    fire_dedup.mark_fired("self_health", "abc", 6 * 3600)
    (_, ttl), = store.values()
    assert ttl == 6 * 3600


def test_fails_open_without_redis(monkeypatch):
    """Нет Redis — отправляем. Фильтр шума не должен терять сигнал."""
    monkeypatch.setattr(fire_dedup, "_redis", lambda: None)
    assert fire_dedup.should_fire("self_health", "abc") is True
    fire_dedup.mark_fired("self_health", "abc", 3600)      # не падает
    fire_dedup.clear("self_health")                        # не падает
    assert fire_dedup.should_fire("self_health", "abc") is True


def test_fails_open_when_redis_raises(monkeypatch):
    """Сломанный Redis тоже не должен глушить отчёт."""
    class Broken:
        def exists(self, k):
            raise RuntimeError("connection reset")

        def setex(self, *a):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(fire_dedup, "_redis", lambda: Broken())
    assert fire_dedup.should_fire("self_health", "abc") is True
    fire_dedup.mark_fired("self_health", "abc", 3600)      # проглочено
