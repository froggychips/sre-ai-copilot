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

import time
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
