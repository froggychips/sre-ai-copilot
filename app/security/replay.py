"""Anti-replay хелперы для подписанных вебхуков.

Подпись (Ed25519 у Discord interactions, HMAC у AlertManager) гарантирует
ПОДЛИННОСТЬ, но не СВЕЖЕСТЬ: валидно подписанный запрос можно переиграть
сколько угодно раз. Для эндпоинтов, которые запускают реальный kubectl
(`apply_confirm_` / `approve:` в Discord), это путь к повторному выполнению
деструктивного действия.

Окно свежести по timestamp — стандартный, дешёвый anti-replay (Discord
прямо рекомендует ±5 мин). Это не заменяет nonce-дедуп (idempotency по
interaction.id), но закрывает основной вектор «перехватил → переиграл позже».

Три уровня защиты AlertManager-вебхука, от сильного к слабому:

  1. signed timestamp (`X-Alertmanager-Timestamp`, HMAC над `ts.body`) —
     ПРЕДПОЧТИТЕЛЬНЫЙ режим: перехваченный запрос протухает вместе с окном
     свежести. Требует signing-proxy перед вебхуком, потому что сам
     Prometheus AlertManager timestamp-заголовок не умеет; включается жёстко
     через ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true.
  2. общий (Redis) nonce-store — закрывает окно между api-репликами: подпись
     принимается ровно один раз на ВЕСЬ деплой, а не по разу на реплику.
  3. локальный per-process TTL-кэш — последний рубеж, работает всегда и без
     сети.

Уровни складываются, а не заменяют друг друга: недоступность Redis
опускает защиту ровно до уровня 3 (как было до этого фикса) и НЕ расширяет
окно replay.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Optional, Protocol, Union

import structlog
from prometheus_client import Counter

log = structlog.get_logger()

REPLAY_REJECTED = Counter(
    "webhook_replay_rejected_total",
    "Signed webhook requests rejected as replays",
    ["source"],  # local = per-process cache, shared = Redis nonce store
)
REPLAY_SHARED_STORE_ERRORS = Counter(
    "webhook_replay_shared_store_errors_total",
    "Shared (Redis) nonce-store checks that failed — anti-replay degraded to per-process",
)


def is_timestamp_fresh(
    timestamp: Union[str, int, float, None],
    max_age_seconds: int,
    now: Optional[float] = None,
) -> bool:
    """True если `timestamp` (unix epoch seconds) в пределах окна от текущего момента.

    Окно двустороннее (abs) — терпит часовой дрейф между signer и сервисом.

    - `max_age_seconds <= 0` → проверка отключена, всегда True (escape hatch).
    - `timestamp` None / не парсится в число → False (нет свежести = отказ).
    - `now` — для детерминированных тестов; по умолчанию `time.time()`.
    """
    if max_age_seconds <= 0:
        return True
    if timestamp is None:
        return False
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= max_age_seconds


class NonceStore(Protocol):
    """Общий для реплик store одноразовых значений.

    `claim` возвращает:
      True  — значение зарегистрировано впервые (не replay);
      False — значение уже было в пределах TTL (replay);
      None  — store недоступен, решение остаётся за вызывающим.
    """

    def claim(
        self, key: str, ttl_seconds: int, now: Optional[float] = None
    ) -> Optional[bool]:  # pragma: no cover - протокол
        ...


class RedisNonceStore:
    """Nonce-store на Redis: `SET <key> 1 NX EX ttl`.

    Зачем: локальный кэш подписей живёт в процессе, поэтому при N api-репликах
    один перехваченный вебхук проигрывается N раз (по разу на реплику). Общий
    ключ в Redis делает подпись одноразовой для всего деплоя.

    Почему СИНХРОННЫЙ клиент: точка вызова —
    `app/api/webhooks.py::verify_alertmanager_signature`, которая обращается к
    кэшу без await (`if ... seen_recently(...)`), и подменить её сигнатуру
    нельзя, не сломав вызывающий код. Чтобы блокирующий вызов не морозил event
    loop api-процесса (механика инцидента 08.08: liveness перестаёт отвечать →
    kubelet убивает поды):
      * socket_timeout / socket_connect_timeout — десятки миллисекунд;
      * после ЛЮБОЙ ошибки store уходит в cooldown на `cooldown_seconds` и в
        этот период вообще не трогает сеть (иначе при лежащем Redis каждый
        вебхук платил бы полный таймаут).

    Fail-*closed* в смысле окна: недоступность store не расширяет окно replay,
    она лишь возвращает защиту к per-process уровню (см. SeenSignatureCache).
    """

    def __init__(
        self,
        url: str,
        *,
        key_prefix: str = "am:replay:nonce:",
        timeout_seconds: float = 0.15,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._key_prefix = key_prefix
        self._timeout_seconds = timeout_seconds
        self._cooldown_seconds = cooldown_seconds
        self._client = None
        self._unavailable_until = 0.0
        self._lock = threading.Lock()

    def _connect(self):
        # Ленивый импорт: sync-клиент нужен только на этом пути.
        import redis

        return redis.Redis.from_url(
            self._url,
            socket_timeout=self._timeout_seconds,
            socket_connect_timeout=self._timeout_seconds,
            decode_responses=True,
        )

    def claim(
        self, key: str, ttl_seconds: int, now: Optional[float] = None
    ) -> Optional[bool]:
        current = time.time() if now is None else now
        with self._lock:
            if current < self._unavailable_until:
                return None
            client = self._client
            if client is None:
                try:
                    client = self._client = self._connect()
                except Exception as e:
                    self._unavailable_until = current + self._cooldown_seconds
                    self._client = None
                    REPLAY_SHARED_STORE_ERRORS.inc()
                    log.warning(
                        "replay.shared_store_unavailable",
                        error=type(e).__name__,
                        message=str(e),
                        cooldown_seconds=self._cooldown_seconds,
                    )
                    return None
        try:
            # NX: первый пришедший забирает подпись себе, остальные видят
            # False. EX: ключ протухает вместе с окном свежести, Redis не
            # копит nonce-ы бесконечно.
            created = client.set(
                self._key_prefix + key, "1", nx=True, ex=max(1, int(ttl_seconds))
            )
        except Exception as e:
            with self._lock:
                self._client = None
                self._unavailable_until = current + self._cooldown_seconds
            REPLAY_SHARED_STORE_ERRORS.inc()
            log.warning(
                "replay.shared_store_unavailable",
                error=type(e).__name__,
                message=str(e),
                cooldown_seconds=self._cooldown_seconds,
            )
            return None
        # redis-py: True если SET состоялся, None если NX не дал записать.
        return bool(created)


def _env_flag(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def shared_nonce_enabled() -> bool:
    """Включён ли общий (Redis) nonce-store для AlertManager-подписей.

    Приоритет: поле настроек ALERTMANAGER_REPLAY_SHARED_NONCE (если его
    когда-нибудь добавят в app/config.py) → одноимённая env-переменная →
    дефолт `settings.is_production`.

    Почему дефолт по проду: несколько api-реплик существуют именно там, и
    только там per-process окно реально даёт ×N replay. В dev/тестах общий
    store выключен намеренно — иначе локально запущенный Redis делал бы
    результат прогона зависимым от ПРЕДЫДУЩЕГО прогона (nonce-ы живут TTL).
    """
    try:
        from app.config import settings
    except Exception:  # pragma: no cover - конфиг недоступен = не включаем
        return False

    configured = getattr(settings, "ALERTMANAGER_REPLAY_SHARED_NONCE", None)
    if configured is not None:
        return bool(configured)
    env = _env_flag("ALERTMANAGER_REPLAY_SHARED_NONCE")
    if env is not None:
        return env
    return bool(getattr(settings, "is_production", False))


_default_store_lock = threading.Lock()
_default_store: Optional[RedisNonceStore] = None


def _default_nonce_store() -> Optional[NonceStore]:
    """Ленивый синглтон RedisNonceStore; None если общий store выключен."""
    if not shared_nonce_enabled():
        return None
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            from app.config import settings

            _default_store = RedisNonceStore(settings.REDIS_URL)
        return _default_store


class SeenSignatureCache:
    """TTL-кэш недавно принятых подписей (anti-replay для body-only HMAC).

    Prometheus AlertManager не умеет слать signed-timestamp заголовки, поэтому
    body-only HMAC сам по себе переигрывается бесконечно: перехваченный
    валидный запрос валиден навсегда. Кэш принимает каждую валидную подпись
    ровно один раз за окно TTL; повтор внутри окна = replay → отказ.
    Легитимные повторы AM (repeat_interval — часы) в окно не попадают.

    Два уровня:
      * локальный OrderedDict — всегда, без сети, bounded (`max_entries`);
      * общий nonce-store (Redis, см. RedisNonceStore) — если включён, делает
        подпись одноразовой для ВСЕХ реплик, а не для одного процесса.

    Порядок важен: сначала дешёвая локальная проверка (она же регистрирует
    подпись), затем общий store. Если store недоступен, поведение ровно то же,
    что было до его появления — per-process окно. Недоступность Redis НЕ
    расширяет окно replay и не превращает проверку в fail-open.
    """

    def __init__(
        self,
        max_entries: int = 4096,
        shared_store: Optional[NonceStore] = None,
        use_default_shared_store: bool = False,
    ) -> None:
        # signature → момент протухания (unix epoch seconds).
        self._entries: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._shared_store = shared_store
        self._use_default_shared_store = use_default_shared_store

    def _resolve_shared_store(self) -> Optional[NonceStore]:
        if self._shared_store is not None:
            return self._shared_store
        if not self._use_default_shared_store:
            return None
        return _default_nonce_store()

    def _seen_locally(self, signature: str, ttl_seconds: int, current: float) -> bool:
        with self._lock:
            # Лениво выкидываем протухшие записи (объём ограничен max_entries,
            # полный проход дёшев).
            expired = [sig for sig, exp in self._entries.items() if exp <= current]
            for sig in expired:
                del self._entries[sig]
            if signature in self._entries:
                return True
            self._entries[signature] = current + ttl_seconds
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return False

    def seen_recently(
        self, signature: str, ttl_seconds: int, now: Optional[float] = None
    ) -> bool:
        """True если `signature` уже принимали в пределах `ttl_seconds` (replay).

        Иначе атомарно регистрирует подпись (локально и, если он включён, в
        общем nonce-store) и возвращает False.
        `ttl_seconds <= 0` → проверка отключена (escape hatch, консистентно
        с is_timestamp_fresh). `now` — для детерминированных тестов.
        """
        if ttl_seconds <= 0:
            return False
        current = time.time() if now is None else now

        if self._seen_locally(signature, ttl_seconds, current):
            REPLAY_REJECTED.labels(source="local").inc()
            return True

        store = self._resolve_shared_store()
        if store is None:
            return False

        claimed = store.claim(signature, ttl_seconds, now=current)
        if claimed is None:
            # Store недоступен: остаёмся на локальном окне — не шире, чем было.
            return False
        if not claimed:
            # Эту подпись уже приняла ДРУГАЯ реплика.
            REPLAY_REJECTED.labels(source="shared").inc()
            return True
        return False

    def clear(self) -> None:
        """Сброс ЛОКАЛЬНОГО кэша (для тестов).

        Общий nonce-store не трогаем: его ключи живут своим TTL и принадлежат
        всему деплою, а не этому процессу.
        """
        with self._lock:
            self._entries.clear()


# Общий кэш для AlertManager webhook (см. verify_alertmanager_signature).
# use_default_shared_store=True — в проде подпись становится одноразовой для
# всех реплик (см. shared_nonce_enabled).
alertmanager_signature_cache = SeenSignatureCache(use_default_shared_store=True)
