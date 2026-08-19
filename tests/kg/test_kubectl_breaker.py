"""Circuit breaker для kubectl: не долбиться в неотвечающий apiserver.

В проекте был брейкер только для LLM — провайдер отвечает деньгами, его
берегли. А kube-apiserver, от которого зависят ВСЕ синки графа, не был
защищён ничем: 23 прямых вызова `subprocess.run` в восьми модулях.

Когда apiserver тупит, тридцать задач из расписания продолжают ходить в него
каждый тик, каждая со своим таймаутом в 15-30 секунд: форки заняты
ожиданием, очередь копится, нагрузка на больной API растёт. Инцидент
19.08.2026 показал соседний вариант — одно незакрытое соединение к apiserver
подвесило CI на 2.5 часа.
"""
from unittest.mock import MagicMock

import pytest

from app.knowledge_graph import kubectl_breaker as cb


@pytest.fixture
def redis(monkeypatch):
    """Поддельный Redis: словарь + счётчики, без сети."""
    store = {}

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def setex(self, k, _ttl, v):
            store[k] = v

        def incr(self, k):
            store[k] = int(store.get(k, 0)) + 1
            return store[k]

        def expire(self, k, _ttl):
            return True

        def delete(self, k):
            store.pop(k, None)

    monkeypatch.setattr(cb, "_redis", lambda: FakeRedis())
    return store


# --- открытие и закрытие --------------------------------------------------


def test_calls_pass_while_apiserver_answers(redis):
    cb.guard_kubectl("get pods")   # не бросает


def test_breaker_opens_after_consecutive_failures(redis):
    """Три подряд — не случайность, а состояние кластера."""
    for _ in range(cb.FAILURES_TO_OPEN):
        cb.record_failure("get pods")

    with pytest.raises(cb.KubectlCircuitOpen):
        cb.guard_kubectl("get pods")


def test_single_failure_does_not_open(redis):
    """Одиночный таймаут бывает при рестарте apiserver и лечится сам."""
    cb.record_failure("get pods")
    cb.guard_kubectl("get pods")   # по-прежнему пропускаем


def test_success_resets_the_counter(redis):
    """Считаем неудачи ПОДРЯД: успех между ними обнуляет счёт."""
    cb.record_failure("get pods")
    cb.record_failure("get pods")
    cb.record_success("get pods")
    cb.record_failure("get pods")

    cb.guard_kubectl("get pods")   # три было, но не подряд


# --- отказ не должен ломать работу ----------------------------------------


def test_missing_redis_allows_calls(monkeypatch):
    """Fail-open: брейкер — оптимизация под сбой, а не новая точка отказа."""
    monkeypatch.setattr(cb, "_redis", lambda: None)
    cb.guard_kubectl("get pods")
    cb.record_failure("get pods")
    cb.record_success("get pods")


def test_broken_redis_does_not_raise(monkeypatch):
    """Redis отвечает ошибкой — синк всё равно должен работать."""
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("redis down")
    broken.incr.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(cb, "_redis", lambda: broken)

    cb.guard_kubectl("get pods")
    cb.record_failure("get pods")


# --- тип ошибки различает две разные ситуации -----------------------------


def test_open_circuit_has_its_own_exception_type(redis):
    """«apiserver не ответил» и «мы решили не спрашивать» — разные события.

    Смешав их, потеряем возможность отличить проблему кластера от нашей
    собственной защиты.
    """
    for _ in range(cb.FAILURES_TO_OPEN):
        cb.record_failure("get ns")

    with pytest.raises(cb.KubectlCircuitOpen) as e:
        cb.guard_kubectl("get ns")
    assert "circuit open" in str(e.value)
    assert issubclass(cb.KubectlCircuitOpen, RuntimeError)


def test_pause_is_shorter_than_the_most_frequent_tick():
    """Иначе брейкер «залипнет» на несколько циклов расписания.

    Самая частая задача идёт раз в минуту; пауза должна укладываться в неё,
    чтобы восстановление проверялось на ближайшем тике.
    """
    assert cb.OPEN_SECONDS <= 60


# --- подключение к синкам -------------------------------------------------


@pytest.mark.parametrize("module,func", [
    ("app.knowledge_graph.k8s_endpoints_sync", "_fetch_endpoints"),
    ("app.knowledge_graph.namespace_lifecycle", "_fetch_namespaces"),
    ("app.knowledge_graph.k8s_ingress_sync", "_kubectl_get_ingresses_all"),
])
def test_heavy_fetchers_ask_the_breaker(module, func):
    """Брейкер бесполезен, пока его никто не спрашивает."""
    import importlib
    import inspect

    src = inspect.getsource(getattr(importlib.import_module(module), func))
    assert "guard_kubectl" in src, (
        f"{module}.{func} ходит в apiserver, не спросив брейкер"
    )
