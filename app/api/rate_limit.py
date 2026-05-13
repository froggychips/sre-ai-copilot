"""Redis-backed rate limiter для /webhooks/alertmanager.

In-memory вариант (defaultdict в процессе) не работает с двумя репликами
api в Helm-чарте: каждый процесс считает свой счётчик, реальный лимит
оказывается N×limit. Здесь — общий счётчик в Redis.

Алгоритм: fixed window 60s.
  ключ: `rl:alertmanager:{client_ip}:{floor(now/60)}`
  INCR на каждый запрос; EXPIRE 70s на первом INCR (чтобы ключи сами
  исчезали из Redis и не накапливались). Если счётчик > limit — 429.

Fail-open: если Redis недоступен — пропускаем запрос с warning-логом.
Rate-limit инфра не должна валить boot AlertManager-потока в условиях
сетевых проблем; auth остаётся через HMAC-подпись.
"""
from __future__ import annotations

import time
from typing import Optional

import structlog
from redis import asyncio as aioredis

from app.config import settings

log = structlog.get_logger()

ALERTMANAGER_RATE_LIMIT = 10  # requests per minute per IP
WINDOW_SECONDS = 60

_redis: Optional[aioredis.Redis] = None


def _get_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        # decode_responses=True → INCR возвращает int напрямую, без .decode().
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def check_alertmanager(client_ip: str) -> bool:
    """Вернуть True если запрос проходит, False если превышен лимит.

    Fail-open: на любых Redis-ошибках пропускаем запрос.
    """
    if not client_ip:
        return True

    window = int(time.time()) // WINDOW_SECONDS
    key = f"rl:alertmanager:{client_ip}:{window}"

    try:
        client = _get_client()
        count = await client.incr(key)
        if count == 1:
            # Первый INCR в окне — выставляем TTL чуть больше окна,
            # чтобы ключ исчез сам и не копился в Redis.
            await client.expire(key, WINDOW_SECONDS + 10)
        if count > ALERTMANAGER_RATE_LIMIT:
            log.warning(
                "ratelimit.exceeded",
                client_ip=client_ip,
                count=count,
                limit=ALERTMANAGER_RATE_LIMIT,
            )
            return False
        return True
    except Exception as e:
        log.warning("ratelimit.redis_unavailable", error=str(e), client_ip=client_ip)
        return True


async def close() -> None:
    """Aclose Redis-клиент при shutdown-е приложения."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
