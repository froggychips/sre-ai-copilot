"""Circuit breaker для вызовов kubectl: не долбиться в неотвечающий apiserver.

В проекте есть брейкер для LLM (`app/services/resilience.py`), и он там не
случайно: провайдер отвечает деньгами и лимитами, поэтому его берегут. А
kube-apiserver, от которого зависят ВСЕ синки графа, не защищён ничем — 23
прямых вызова `subprocess.run` в восьми модулях.

Что это значит на практике. Когда apiserver тупит, тридцать задач из
расписания продолжают ходить в него каждый тик, каждая со своим таймаутом в
15-30 секунд. Форки заняты ожиданием, очередь копится, а нагрузка на больной
apiserver только растёт. Инцидент 19.08.2026 показал соседний вариант этой
болезни: одно незакрытое соединение к apiserver подвесило CI на 2.5 часа.

Брейкер разрывает петлю: после N подряд неудач вызовы отклоняются сразу, без
похода в сеть, а через TTL пропускается пробный. Синк при этом не падает —
он получает ту же ошибку, что и при обычном сбое, и его собственный deadman
(`edge_decay_guard`, `namespace_lifecycle`) не даёт принять пустой ответ за
факт.

Состояние живёт в Redis, а не в памяти процесса: воркер запущен с
`--concurrency=4`, форки не разделяют память, и in-memory счётчик означал бы
четыре независимых брейкера вместо одного.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

__all__ = ["KubectlCircuitOpen", "guard_kubectl", "record_failure", "record_success"]

#: Сколько подряд неудач открывают брейкер. Три — компромисс: одиночный
#: таймаут бывает при рестарте apiserver и лечится сам, три подряд означают,
#: что проблема не в конкретном запросе.
FAILURES_TO_OPEN = int(os.environ.get("KUBECTL_CB_FAILURES", "3"))

#: Насколько замолкаем. 60 секунд — меньше самого частого тика (kg-external-probe
#: раз в минуту), поэтому брейкер не «залипает» на несколько циклов, но
#: достаточно, чтобы дать apiserver прийти в себя.
OPEN_SECONDS = int(os.environ.get("KUBECTL_CB_OPEN_SECONDS", "60"))

_KEY_FAILURES = "kg:kubectl:cb:failures"
_KEY_OPEN = "kg:kubectl:cb:open"


class KubectlCircuitOpen(RuntimeError):
    """Брейкер открыт — вызов отклонён без похода в сеть.

    Отдельный тип, а не общий RuntimeError: вызывающий должен уметь отличить
    «apiserver не ответил» от «мы сами решили не спрашивать». Первое —
    сигнал о кластере, второе — о нас, и в метриках их смешивать нельзя.
    """


def _redis():
    """Клиент Redis или None. Недоступность Redis НЕ должна ломать синки."""
    try:
        import redis

        from app.config import settings
        return redis.from_url(settings.REDIS_URL, socket_timeout=2)
    except Exception as e:  # noqa: BLE001 — брейкер вторичен по отношению к работе
        log.debug("kubectl_cb.redis_unavailable: %s", e)
        return None


def guard_kubectl(operation: str = "kubectl") -> None:
    """Бросить `KubectlCircuitOpen`, если брейкер открыт. Иначе — пропустить.

    Fail-open по отношению к Redis: если хранилища состояния нет, вызов
    разрешается. Брейкер — оптимизация под сбой, и превращать его в
    дополнительную точку отказа нельзя.
    """
    r = _redis()
    if r is None:
        return
    try:
        if r.get(_KEY_OPEN):
            raise KubectlCircuitOpen(
                f"{operation}: circuit open — apiserver не отвечал "
                f"{FAILURES_TO_OPEN} раза подряд, ждём {OPEN_SECONDS}s"
            )
    except KubectlCircuitOpen:
        raise
    except Exception as e:  # noqa: BLE001
        log.debug("kubectl_cb.check_failed: %s", e)


def record_failure(operation: str = "kubectl") -> None:
    """Учесть неудачу; на пороге — открыть брейкер."""
    r = _redis()
    if r is None:
        return
    try:
        fails = r.incr(_KEY_FAILURES)
        # TTL на счётчике: редкие одиночные сбои не должны накапливаться
        # неделями и однажды сложиться в «три подряд».
        r.expire(_KEY_FAILURES, OPEN_SECONDS * 2)
        if fails >= FAILURES_TO_OPEN:
            r.setex(_KEY_OPEN, OPEN_SECONDS, "1")
            r.delete(_KEY_FAILURES)
            log.warning(
                "kubectl_cb.opened operation=%s failures=%s pause=%ss",
                operation, fails, OPEN_SECONDS,
            )
    except Exception as e:  # noqa: BLE001
        log.debug("kubectl_cb.record_failure_failed: %s", e)


def record_success(operation: str = "kubectl") -> None:
    """Успех обнуляет счётчик: считаем неудачи ПОДРЯД, а не всего."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_KEY_FAILURES)
    except Exception as e:  # noqa: BLE001
        log.debug("kubectl_cb.record_success_failed: %s", e)


def circuit_is_open() -> bool:
    """Для проверок и тестов: открыт ли брейкер сейчас."""
    r = _redis()
    if r is None:
        return False
    try:
        return bool(r.get(_KEY_OPEN))
    except Exception:  # noqa: BLE001
        return False
