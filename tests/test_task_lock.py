"""Singleton-локи: второй экземпляр beat-задачи не стартует поверх первого.

Прод, 14.08.2026: в `unacked` одновременно висели по два `kg_storage_sync` и
`kg_anomaly_detection_task` — расписания */30 и */10, а прогоны переросли свой
интервал. Beat шлёт задачу, не спрашивая, закончилась ли предыдущая; копии
конкурировали за одни и те же строки под row-lock, занимали шесть слотов из
восьми, и `kg_topology_sync` ждал свободный слот час — при собственном времени
выполнения 95 секунд.

Ключевые свойства, которые тут охраняются:
  * второй запуск возвращает skipped, а не выполняет тело;
  * пропуск НЕ пишет heartbeat (иначе deadman скажет «синк живой» ровно
    тогда, когда он завис);
  * недоступный Redis не останавливает синки (fail-open);
  * лок снимается только своим владельцем.
"""
import pytest

from app.workers import task_lock
from app.workers.task_lock import (LOCK_KEY_PREFIX, SKIPPED_LOCKED,
                                   is_skipped, single_instance)


class FakeRedis:
    """Минимальный Redis с семантикой SET NX EX."""

    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis недоступен")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis недоступен")
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    def _install(client=None):
        client = client if client is not None else FakeRedis()
        monkeypatch.setattr(task_lock, "_redis_client", lambda: client)
        return client
    return _install


# --- основное свойство ----------------------------------------------------


def test_second_run_is_skipped_while_first_holds_lock(redis):
    """Ровно тот сценарий с прода: прогон длиннее интервала."""
    redis()
    calls = []

    @single_instance(ttl_seconds=60)
    def kg_heavy_sync():
        calls.append(1)
        # Пока «первый» внутри — beat присылает второй по расписанию.
        second = kg_heavy_sync()
        assert is_skipped(second), "второй экземпляр обязан быть пропущен"
        return {"ok": True}

    result = kg_heavy_sync()
    assert result == {"ok": True}
    assert len(calls) == 1, "тело задачи выполнилось дважды — лок не сработал"


def test_lock_released_allows_next_run(redis):
    """После завершения следующий прогон проходит — лок не залипает."""
    redis()
    runs = []

    @single_instance(ttl_seconds=60)
    def sync():
        runs.append(1)
        return {"ok": True}

    sync()
    sync()
    assert len(runs) == 2


def test_skip_marker_is_recognisable():
    assert is_skipped({"skipped": SKIPPED_LOCKED}) is True
    assert is_skipped({"skipped": "other"}) is False
    assert is_skipped({"error": "boom"}) is False
    assert is_skipped(None) is False


def test_lock_key_is_per_task(redis):
    """Разные задачи не блокируют друг друга."""
    redis()

    @single_instance(ttl_seconds=60)
    def sync_a():
        return {"who": "a", "b": sync_b()}

    @single_instance(ttl_seconds=60)
    def sync_b():
        return {"who": "b"}

    result = sync_a()
    assert result["b"] == {"who": "b"}, "чужая задача не должна блокироваться"


# --- fail-open ------------------------------------------------------------


def test_without_redis_task_still_runs(monkeypatch):
    """Недоступный Redis не должен останавливать синки."""
    monkeypatch.setattr(task_lock, "_redis_client", lambda: None)
    runs = []

    @single_instance()
    def sync():
        runs.append(1)
        return {"ok": True}

    assert sync() == {"ok": True}
    assert runs == [1]


def test_redis_error_on_acquire_does_not_block(redis):
    """Ошибка при взятии лока — работаем как раньше, а не падаем."""
    redis(FakeRedis(fail=True))
    runs = []

    @single_instance()
    def sync():
        runs.append(1)
        return {"ok": True}

    assert sync() == {"ok": True}
    assert runs == [1]


def test_task_exception_releases_lock(redis):
    """Падение задачи не оставляет лок висеть до TTL."""
    client = redis()

    @single_instance(ttl_seconds=60)
    def sync():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sync()
    assert f"{LOCK_KEY_PREFIX}sync" not in client.store, "лок не снят после падения"


# --- владение локом -------------------------------------------------------


def test_foreign_lock_is_not_released(redis):
    """Свой прогон пережил TTL, лок достался другому — чужой не снимаем.

    Иначе вернулась бы ровно та параллельность, ради устранения которой
    всё это написано.
    """
    client = redis()

    @single_instance(ttl_seconds=60)
    def sync():
        # Имитируем: TTL истёк, лок перехватил другой экземпляр.
        client.store[f"{LOCK_KEY_PREFIX}sync"] = "чужой-токен"
        return {"ok": True}

    sync()
    assert client.store.get(f"{LOCK_KEY_PREFIX}sync") == "чужой-токен"
    assert client.deleted == [], "снят чужой лок"


# --- связь с heartbeat ----------------------------------------------------


def test_heartbeat_not_written_for_skipped_run(monkeypatch):
    """Пропуск не должен выглядеть как успешный прогон.

    `check_sync_lag` в self_health смотрит именно на heartbeat: если писать
    его при пропуске, deadman отрапортует «синк ходит» в тот самый момент,
    когда предыдущий экземпляр завис.
    """
    from app.workers import tasks

    written = []
    monkeypatch.setattr(
        "app.services.stats_digest._record_task_heartbeat",
        lambda name, ts=None: written.append(name),
    )

    tasks._record_beat_heartbeat(
        task=type("T", (), {"name": "kg_metrics_sync"})(),
        state="SUCCESS",
        retval={"skipped": SKIPPED_LOCKED, "task": "kg_metrics_sync"},
    )
    assert written == [], "heartbeat записан для пропущенного прогона"


def test_heartbeat_written_for_real_run(monkeypatch):
    from app.workers import tasks

    written = []
    monkeypatch.setattr(
        "app.services.stats_digest._record_task_heartbeat",
        lambda name, ts=None: written.append(name),
    )

    tasks._record_beat_heartbeat(
        task=type("T", (), {"name": "kg_metrics_sync"})(),
        state="SUCCESS",
        retval={"services": 42},
    )
    assert written == ["kg_metrics_sync"]
