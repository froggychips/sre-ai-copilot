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
"""
from __future__ import annotations

import functools
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

# TTL лока по умолчанию. Смысл: страховка от вечной блокировки, если воркер
# умер, не сняв лок (SIGKILL, OOM, потеря пода). Держим заведомо больше
# самого долгого штатного прогона, но конечным — иначе одна смерть воркера
# заблокировала бы задачу навсегда.
DEFAULT_TTL_SECONDS = 3600


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

    ttl_seconds: срок жизни лока. Ставить больше самого долгого прогона —
        иначе второй экземпляр стартует поверх живого первого и мы вернёмся
        к тому, от чего лечимся.
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
            try:
                acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
            except Exception as e:  # noqa: BLE001
                log.warning("task_lock.acquire_failed", task=lock_name, error=str(e))
                return fn(*args, **kwargs)

            if not acquired:
                log.info("task_lock.skipped_already_running", task=lock_name)
                return {"skipped": SKIPPED_LOCKED, "task": lock_name}

            try:
                return fn(*args, **kwargs)
            finally:
                _release(client, key, token, lock_name)

        return wrapper
    return decorator


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
