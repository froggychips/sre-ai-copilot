"""Redis-backed rate limiter для /webhooks/alertmanager.

In-memory вариант (defaultdict в процессе) не работает с двумя репликами
api в Helm-чарте: каждый процесс считает свой счётчик, реальный лимит
оказывается N×limit. Здесь — общий счётчик в Redis.

Алгоритм: fixed window 60s.
  ключ: `rl:alertmanager:{client_ip}:{floor(now/60)}`
  INCR на каждый запрос; EXPIRE 70s на первом INCR (чтобы ключи сами
  исчезали из Redis и не накапливались). Если счётчик > limit — 429.

Деградация при недоступном Redis (было — полный fail-open):
  Лимит НЕ снимается совсем, а падает на in-process fixed-window счётчик с
  тем же порогом. Это осознанно слабее общего счётчика (каждая реплика
  считает своё окно → потолок N×limit вместо limit), но принципиально
  сильнее прежнего «пропускаем всё»: одиночный источник флуда больше не
  получает неограниченный доступ к пайплайну, пока Redis лежит.
  Auth при этом всё равно держится на HMAC-подписи (см. webhooks.py).

Наблюдаемость: каждая деградация считается в prometheus-метрики
  `rate_limit_redis_errors_total{limiter="alertmanager"}` и
  `rate_limit_local_fallback_total{limiter="alertmanager",decision=...}`,
плюс warning-лог, ДРОССЕЛИРОВАННЫЙ до одного раза в минуту: при лежащем
Redis лог на каждый запрос — это ровно тот флуд, который забивает Seq
(диск-гвард Seq не работает, см. постмортемы).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

import structlog
from prometheus_client import Counter
from redis import asyncio as aioredis

from app.config import settings

log = structlog.get_logger()

ALERTMANAGER_RATE_LIMIT = 10  # requests per minute per IP
WINDOW_SECONDS = 60

# Сколько (ip, window)-ключей держим в in-process fallback-счётчике. Ключи
# живут одно окно, но при флуде с разных IP словарь без границы — это утечка
# памяти в api-поде; вытесняем старейшие.
_FALLBACK_MAX_KEYS = 8192

# Дроссель warning-лога о недоступности Redis (сек).
_REDIS_WARN_INTERVAL_SECONDS = 60.0

RATE_LIMIT_REDIS_ERRORS = Counter(
    "rate_limit_redis_errors_total",
    "Rate-limit checks that could not reach Redis (degraded to in-process limit)",
    ["limiter"],
)
RATE_LIMIT_LOCAL_FALLBACK = Counter(
    "rate_limit_local_fallback_total",
    "Rate-limit decisions taken by the in-process fallback limiter",
    ["limiter", "decision"],
)

_redis: Optional[aioredis.Redis] = None

# (client_ip, window) → счётчик запросов. Используется ТОЛЬКО когда Redis
# недоступен. Один процесс = один event loop, отдельный лок не нужен.
_fallback_counters: "OrderedDict[tuple[str, int], int]" = OrderedDict()

_redis_warn_state = {"last_logged": 0.0, "suppressed": 0}


def _get_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        # decode_responses=True → INCR возвращает int напрямую, без .decode().
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _warn_redis_unavailable(error: str, client_ip: str) -> None:
    """Warning о деградации лимитера, не чаще одного раза в минуту.

    Подавленные срабатывания не теряются: их число печатается следующим
    прошедшим дроссель логом (и всегда доступно в метрике).
    """
    now = time.monotonic()
    if now - _redis_warn_state["last_logged"] < _REDIS_WARN_INTERVAL_SECONDS:
        _redis_warn_state["suppressed"] += 1
        return
    log.warning(
        "ratelimit.redis_unavailable",
        error=error,
        client_ip=client_ip,
        fallback="in_process_window",
        suppressed_since_last_log=_redis_warn_state["suppressed"],
    )
    _redis_warn_state["last_logged"] = now
    _redis_warn_state["suppressed"] = 0


def _check_local_fallback(client_ip: str, window: int) -> bool:
    """In-process fixed-window лимит с тем же порогом. True = пропускаем.

    Потолок — limit НА РЕПЛИКУ (реплики не видят друг друга), то есть
    N×limit на кластер. Хуже общего счётчика, но это ограниченная деградация,
    а не отключение лимита.
    """
    key = (client_ip, window)
    count = _fallback_counters.get(key, 0) + 1
    _fallback_counters[key] = count
    _fallback_counters.move_to_end(key)
    while len(_fallback_counters) > _FALLBACK_MAX_KEYS:
        _fallback_counters.popitem(last=False)

    if count > ALERTMANAGER_RATE_LIMIT:
        RATE_LIMIT_LOCAL_FALLBACK.labels(
            limiter="alertmanager", decision="blocked"
        ).inc()
        log.warning(
            "ratelimit.exceeded_local_fallback",
            client_ip=client_ip,
            count=count,
            limit=ALERTMANAGER_RATE_LIMIT,
        )
        return False
    RATE_LIMIT_LOCAL_FALLBACK.labels(limiter="alertmanager", decision="allowed").inc()
    return True


async def check_alertmanager(client_ip: str) -> bool:
    """Вернуть True если запрос проходит, False если превышен лимит.

    При недоступном Redis лимит не снимается: решение принимает in-process
    fallback-счётчик (см. _check_local_fallback), деградация видна в метриках.
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
        RATE_LIMIT_REDIS_ERRORS.labels(limiter="alertmanager").inc()
        _warn_redis_unavailable(str(e), client_ip)
        return _check_local_fallback(client_ip, window)


def reset_local_fallback() -> None:
    """Сброс in-process счётчика и дросселя лога (для тестов)."""
    _fallback_counters.clear()
    _redis_warn_state["last_logged"] = 0.0
    _redis_warn_state["suppressed"] = 0


async def close() -> None:
    """Aclose Redis-клиент при shutdown-е приложения."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
