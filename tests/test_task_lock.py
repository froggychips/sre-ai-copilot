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
        self.expired = []
        self.set_ex = []

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis недоступен")
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.set_ex.append(ex)
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis недоступен")
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)

    def expire(self, key, seconds):
        if self.fail:
            raise ConnectionError("redis недоступен")
        self.expired.append((key, seconds))
        return key in self.store


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


# --- аренда: смерть воркера не съедает следующий запуск ----------------------


def test_lock_is_set_with_short_lease_not_full_ttl(redis):
    """16.10.2026: лок на 3600 с пережил убитый деплоем прогон и съел запуск
    09:00. В Redis теперь лежит аренда — столько лок живёт после смерти."""
    client = redis()

    @single_instance(ttl_seconds=3600)
    def kg_hourly():
        return "ok"

    kg_hourly()
    assert client.set_ex == [task_lock.LEASE_SECONDS]
    assert task_lock.LEASE_SECONDS < 300, "аренда должна быть короче самого частого интервала"


def test_lease_is_not_longer_than_ttl(redis):
    client = redis()

    @single_instance(ttl_seconds=30)
    def kg_short():
        return "ok"

    kg_short()
    assert client.set_ex == [30]
    assert kg_short.lock_lease_seconds == 30


def test_renew_extends_own_lock():
    client = FakeRedis()
    client.store["k"] = "tok"
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 3600, "t")
    assert r.renew_once() is True
    assert client.expired == [("k", 90)]


def test_renew_stops_when_lock_is_foreign():
    """Лок истёк и перехвачен — чужой не продлеваем."""
    client = FakeRedis()
    client.store["k"] = "someone-else"
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 3600, "t")
    assert r.renew_once() is False
    assert client.expired == []


def test_renew_stops_after_max_hold(monkeypatch):
    """Потолок удержания: зависший (не умерший) прогон не держит лок вечно."""
    client = FakeRedis()
    client.store["k"] = "tok"
    now = [1000.0]
    monkeypatch.setattr(task_lock.time, "monotonic", lambda: now[0])
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 600, "t")
    now[0] += 601
    assert r.renew_once() is False
    assert client.expired == []


def test_renew_redis_error_does_not_raise():
    client = FakeRedis(fail=True)
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 3600, "t")
    assert r.renew_once() is True  # попробуем ещё раз на следующем тике


def test_renewer_thread_stops_with_task(redis, monkeypatch):
    """Поток продления завершается вместе с прогоном, лок снят."""
    client = redis()
    monkeypatch.setattr(task_lock, "_RENEW_EVERY_SECONDS", 0.01)
    import threading
    import time as _t

    @single_instance(ttl_seconds=3600)
    def kg_slowish():
        _t.sleep(0.05)
        return "ok"

    assert kg_slowish() == "ok"
    assert client.expired, "живой прогон должен продлевать аренду"
    assert not [t for t in threading.enumerate() if t.name == "task-lock-kg_slowish"]
    assert f"{LOCK_KEY_PREFIX}kg_slowish" not in client.store


def test_renew_never_extends_past_max_hold(monkeypatch):
    """На краю потолка продлеваем только на остаток (ревью Codex): у
    kg_incidents_lifecycle ttl=240 при интервале 300 полная аренда на 210-й
    секунде держала бы лок до ~300-й."""
    client = FakeRedis()
    client.store["k"] = "tok"
    now = [0.0]
    monkeypatch.setattr(task_lock.time, "monotonic", lambda: now[0])
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 240, "t")
    now[0] = 210.0
    assert r.renew_once() is True
    assert client.expired == [("k", 30)]


class EvalRedis(FakeRedis):
    """Redis с серверным compare-and-expire (как настоящий)."""

    def __init__(self):
        super().__init__()
        self.evals = []

    def eval(self, script, numkeys, key, token, seconds):
        self.evals.append((key, token, seconds))
        if self.store.get(key) == token:
            self.expired.append((key, seconds))
            return 1
        return 0


def test_renew_uses_atomic_compare_and_expire():
    client = EvalRedis()
    client.store["k"] = "tok"
    r = task_lock._LeaseRenewer(client, "k", "tok", 90, 3600, "t")
    assert r.renew_once() is True
    assert client.evals == [("k", "tok", 90)]
    client.store["k"] = "other"
    assert r.renew_once() is False
