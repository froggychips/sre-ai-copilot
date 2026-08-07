"""Anti-replay хелперы для подписанных вебхуков.

Подпись (Ed25519 у Discord interactions, HMAC у AlertManager) гарантирует
ПОДЛИННОСТЬ, но не СВЕЖЕСТЬ: валидно подписанный запрос можно переиграть
сколько угодно раз. Для эндпоинтов, которые запускают реальный kubectl
(`apply_confirm_` / `approve:` в Discord), это путь к повторному выполнению
деструктивного действия.

Окно свежести по timestamp — стандартный, дешёвый anti-replay (Discord
прямо рекомендует ±5 мин). Это не заменяет nonce-дедуп (idempotency по
interaction.id), но закрывает основной вектор «перехватил → переиграл позже».
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional, Union


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


class SeenSignatureCache:
    """TTL-кэш недавно принятых подписей (anti-replay для body-only HMAC).

    Prometheus AlertManager не умеет слать signed-timestamp заголовки, поэтому
    body-only HMAC сам по себе переигрывается бесконечно: перехваченный
    валидный запрос валиден навсегда. Кэш принимает каждую валидную подпись
    ровно один раз за окно TTL; повтор внутри окна = replay → отказ.
    Легитимные повторы AM (repeat_interval — часы) в окно не попадают.

    In-memory и per-process: несколько api-реплик держат независимые окна.
    Это слабее глобального nonce в Redis, но fail-closed auth не должен
    зависеть от доступности Redis (rate-limit рядом сознательно fail-open).
    Bounded: не больше `max_entries` записей, старейшие вытесняются.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        # signature → момент протухания (unix epoch seconds).
        self._entries: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def seen_recently(
        self, signature: str, ttl_seconds: int, now: Optional[float] = None
    ) -> bool:
        """True если `signature` уже принимали в пределах `ttl_seconds` (replay).

        Иначе атомарно регистрирует подпись и возвращает False.
        `ttl_seconds <= 0` → проверка отключена (escape hatch, консистентно
        с is_timestamp_fresh). `now` — для детерминированных тестов.
        """
        if ttl_seconds <= 0:
            return False
        current = time.time() if now is None else now
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

    def clear(self) -> None:
        """Сброс кэша (для тестов)."""
        with self._lock:
            self._entries.clear()


# Общий кэш для AlertManager webhook (см. verify_alertmanager_signature).
alertmanager_signature_cache = SeenSignatureCache()
