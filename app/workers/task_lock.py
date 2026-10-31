"""Singleton-локи для beat-задач: не запускать второй экземпляр поверх первого.

Celery beat шлёт задачу по расписанию, не спрашивая, закончилась ли предыдущая.
Пока прогон короче интервала, это незаметно. Когда длиннее — экземпляры
накладываются и начинают конкурировать за одни и те же строки: запись в
kg_services идёт под row-lock и per-namespace commit, поэтому копии ждут локов
друг друга и вместе идут в разы дольше, чем шёл бы один.

Замер 14.08.2026 (прод): в `unacked` одновременно висели по два
`kg_storage_sync` и `kg_anomaly_detection_task` при расписаниях */30 и */10 —
то есть прогоны переросли свой интервал. Шесть из восьми слотов воркеров были
заняты, и `kg_topology_sync` ждал свободный слот **час**, хотя сам
выполняется 95 секунд. Со стороны это выглядело как «синк не укладывается в
окно», хотя проблема была ровно обратной: он не мог начаться.

Лок намеренно **fail-open**: недоступный Redis не должен останавливать синки —
без лока система работает как раньше, а не встаёт.

Аренда, а не долгий TTL (16.10.2026). Раньше лок ставился сразу на
`ttl_seconds` (у часового kg_topology_sync — 3600) и жил столько же после
смерти воркера. Деплой в 08:01 убил прогон 08:00 (grace 30 с), лок остался, и
запуск 09:00:00 получил `already_running` — лок истекал в ту же секунду. Сам
синк идёт ~2 минуты, а потерян был час. У 10 из 17 задач TTL был ≥ интервала
расписания, то есть любая смерть воркера гарантированно съедала следующий запуск.

Теперь в Redis лежит короткая аренда (`LEASE_SECONDS`), и пока задача жива,
фоновый поток её продлевает. Живой прогон лок не теряет, сколько бы ни шёл
(вплоть до `ttl_seconds` — потолка удержания); умерший освобождает его через
LEASE_SECONDS, а не через час.
"""
from __future__ import annotations

import functools
import threading
import time
import uuid
from typing import Any, Callable, Optional

import structlog

log = structlog.get_logger()

__all__ = ["single_instance", "LOCK_KEY_PREFIX", "SKIPPED_LOCKED"]

LOCK_KEY_PREFIX = "celery:lock:"

#: Маркер в возврате задачи: прогон пропущен, потому что предыдущий ещё идёт.
#: Не ошибка — по нему НЕ пишется heartbeat (см. tasks._record_beat_heartbeat):
#: пропуск не означает, что задача отработала, иначе deadman рапортовал бы
#: «синк живой» ровно в тот момент, когда он завис.
SKIPPED_LOCKED = "already_running"

# Потолок удержания лока по умолчанию: дольше живой прогон лок не продлевает
# (страховка от зависшего, но не умершего процесса). Держим заведомо больше
# самого долгого штатного прогона.
DEFAULT_TTL_SECONDS = 3600

#: Срок аренды в Redis — столько лок переживает смерть своего воркера
#: (SIGKILL, OOM, потеря пода). ОБЯЗАН быть меньше интервала расписания
#: каждой задачи с локом, иначе смерть воркера снова съедает следующий
#: запуск — сторожит tests/test_task_lock_ttl_vs_schedule.py. С 31.10.2026
#: лок стоит на всех beat-задачах, включая минутную kg_external_probe: у
#: неё ttl=50, и аренда (min(LEASE, ttl)) короче тика сама по себе.
LEASE_SECONDS = 90

#: Как часто продлевать аренду: с запасом в два пропуска подряд (сеть, GIL).
_RENEW_EVERY_SECONDS = LEASE_SECONDS / 3


def _redis_client():
    """Sync-клиент heartbeat-слоя. None при любой проблеме — лок fail-open."""
    try:
        from app.services.digest.state import _get_beat_redis
        return _get_beat_redis()
    except Exception as e:  # noqa: BLE001 — лок не должен ронять задачу
        log.warning("task_lock.redis_unavailable", error=type(e).__name__)
        return None


def single_instance(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    name: Optional[str] = None,
) -> Callable:
    """Не давать задаче запуститься, пока идёт её предыдущий экземпляр.

    ttl_seconds: потолок удержания лока живым прогоном. Ставить больше самого
        долгого прогона — иначе второй экземпляр стартует поверх живого
        первого и мы вернёмся к тому, от чего лечимся. После смерти воркера
        лок живёт не ttl_seconds, а LEASE_SECONDS.
    name: имя лока; по умолчанию имя функции. Указывать явно, если один и тот
        же лок должен разделяться несколькими задачами.

    Пропуск возвращает `{"skipped": SKIPPED_LOCKED}` и НЕ считается ошибкой:
    расписание своё возьмёт на следующем тике.
    """
    def decorator(fn: Callable) -> Callable:
        lock_name = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            client = _redis_client()
            if client is None:
                # Fail-open: без Redis работаем как до появления локов.
                return fn(*args, **kwargs)

            key = f"{LOCK_KEY_PREFIX}{lock_name}"
            token = uuid.uuid4().hex
            lease = min(LEASE_SECONDS, ttl_seconds)
            try:
                acquired = bool(client.set(key, token, nx=True, ex=lease))
            except Exception as e:  # noqa: BLE001
                log.warning("task_lock.acquire_failed", task=lock_name, error=str(e))
                return fn(*args, **kwargs)

            if not acquired:
                log.info("task_lock.skipped_already_running", task=lock_name)
                return {"skipped": SKIPPED_LOCKED, "task": lock_name}

            renewer = _LeaseRenewer(client, key, token, lease, ttl_seconds, lock_name)
            renewer.start()
            try:
                return fn(*args, **kwargs)
            finally:
                renewer.stop()
                _release(client, key, token, lock_name)

        # Для инвариант-теста «лок после смерти воркера живёт меньше интервала
        # расписания» (tests/test_task_lock_ttl_vs_schedule.py).
        wrapper.lock_ttl_seconds = ttl_seconds  # type: ignore[attr-defined]
        wrapper.lock_lease_seconds = min(LEASE_SECONDS, ttl_seconds)  # type: ignore[attr-defined]
        wrapper.lock_name = lock_name  # type: ignore[attr-defined]
        return wrapper
    return decorator


class _LeaseRenewer:
    """Фоновый поток: продлевает аренду лока, пока прогон жив.

    Продлевает только СВОЙ лок (по токену) и только до `max_hold` — потолка
    удержания. Потерял лок (истёк, перехвачен) — перестаёт продлевать и пишет
    warning, прогон не прерывает: лок здесь — защита от наложения, а не
    взаимоисключение ценой корректности. Ошибки Redis не роняют задачу.

    Поток daemon: процесс, убитый SIGKILL/hard time limit, уносит его с собой,
    и аренда истекает сама — ради этого всё и устроено.
    """

    def __init__(self, client, key: str, token: str, lease: int,
                 max_hold: int, lock_name: str) -> None:
        self._client = client
        self._key = key
        self._token = token
        self._lease = lease
        self._deadline = time.monotonic() + max_hold
        self._lock_name = lock_name
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"task-lock-{lock_name}", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(_RENEW_EVERY_SECONDS):
            if not self.renew_once():
                return

    def renew_once(self) -> bool:
        """Одно продление. False — продлевать больше не нужно."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            log.warning("task_lock.max_hold_reached", task=self._lock_name)
            return False
        # Не дальше потолка: иначе продление на его краю держало бы лок до
        # deadline + lease — у задачи с ttl близким к интервалу снова съедая
        # следующий запуск (ревью Codex).
        seconds = max(1, min(self._lease, int(remaining)))
        try:
            owned = _compare_and_expire(self._client, self._key, self._token, seconds)
        except Exception as e:  # noqa: BLE001 — продление best-effort
            log.warning("task_lock.renew_failed", task=self._lock_name, error=str(e))
            return True
        if not owned:
            log.warning("task_lock.lease_lost", task=self._lock_name)
            return False
        return True


#: Продлить лок, только если он всё ещё наш, — одной командой на сервере.
#: GET + EXPIRE раздельно оставляли окно: аренда истекла и досталась другому
#: прогону, а старый поток продлевал бы уже чужой лок.
_COMPARE_AND_EXPIRE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)


def _compare_and_expire(client, key: str, token: str, seconds: int) -> bool:
    """True — лок наш и продлён. Без `eval` у клиента — неатомарный путь."""
    if hasattr(client, "eval"):
        return bool(client.eval(_COMPARE_AND_EXPIRE_LUA, 1, key, token, seconds))
    if client.get(key) != token:
        return False
    client.expire(key, seconds)
    return True


def _release(client, key: str, token: str, lock_name: str) -> None:
    """Снять ТОЛЬКО свой лок.

    Проверка токена обязательна: если наш прогон пережил TTL, лок уже мог
    достаться другому экземпляру — и слепой DELETE снял бы чужой, вернув ровно
    ту параллельность, ради устранения которой всё это написано.

    Проверка и удаление не атомарны (без Lua): между GET и DELETE лок теоретически
    может смениться. Окно — микросекунды против TTL в час, а цена ошибки —
    один лишний параллельный прогон, то есть возврат к прежнему поведению.
    Lua-скрипт здесь был бы строже, но потребовал бы совместимости со всеми
    redis-клиентами, которые подменяют тесты.
    """
    try:
        current = client.get(key)
        if current == token:
            client.delete(key)
        elif current is not None:
            log.warning("task_lock.foreign_lock_not_released", task=lock_name)
    except Exception as e:  # noqa: BLE001 — снятие лока best-effort
        log.warning("task_lock.release_failed", task=lock_name, error=str(e))


def is_skipped(result: Any) -> bool:
    """Был ли прогон пропущен из-за лока (для heartbeat и отчётов)."""
    return isinstance(result, dict) and result.get("skipped") == SKIPPED_LOCKED
