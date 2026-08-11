"""Недоступность Redis больше не СНИМАЕТ rate-limit на /webhooks/alertmanager.

Раньше это был полный fail-open: любая ошибка Redis → `return True`, то есть
при лежащем Redis один источник получал неограниченный доступ к вебхуку
(auth держался только на HMAC). Осознанное решение, но не наблюдаемое и не
ограниченное.

Теперь деградация ограничена и видна:
  * решение принимает in-process fixed-window счётчик с ТЕМ ЖЕ порогом
    (потолок становится N×limit по числу реплик, а не бесконечность);
  * каждая деградация считается в prometheus-метрики;
  * warning дросселируется до 1/мин, чтобы лежащий Redis не залил Seq
    (диск-гвард Seq не работает — см. постмортемы).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from app.api import rate_limit


@pytest.fixture(autouse=True)
def _reset_fallback():
    """In-process счётчик — глобальный на процесс; изолируем тесты."""
    rate_limit.reset_local_fallback()
    yield
    rate_limit.reset_local_fallback()


def _dead_redis():
    """Клиент, который падает на любой команде."""

    async def _boom(*_a, **_kw):
        raise ConnectionError("redis unavailable")

    return SimpleNamespace(incr=_boom, expire=_boom)


def _live_redis():
    """Клиент, эмулирующий server-side INCR (общий счётчик)."""
    state: dict[str, int] = {}

    async def _incr(key):
        state[key] = state.get(key, 0) + 1
        return state[key]

    async def _expire(_key, _ttl):
        return True

    return SimpleNamespace(incr=_incr, expire=_expire)


def _metric(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_limit_survives_redis_outage():
    """Лимит не снят: 10 запросов проходят, 11-й отлуп даже без Redis."""
    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        results = [
            await rate_limit.check_alertmanager("203.0.113.30") for _ in range(11)
        ]
    assert results[: rate_limit.ALERTMANAGER_RATE_LIMIT] == [True] * 10
    assert results[rate_limit.ALERTMANAGER_RATE_LIMIT] is False


@pytest.mark.asyncio
async def test_flood_from_single_ip_is_bounded_without_redis():
    """100 запросов подряд при лежащем Redis: пропущено ровно limit."""
    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        results = [
            await rate_limit.check_alertmanager("203.0.113.31") for _ in range(100)
        ]
    assert results.count(True) == rate_limit.ALERTMANAGER_RATE_LIMIT


@pytest.mark.asyncio
async def test_fallback_windows_are_per_ip_and_roll_over(monkeypatch):
    """Окно fixed-window: другой IP и следующая минута считаются отдельно."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock["t"])

    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        for _ in range(rate_limit.ALERTMANAGER_RATE_LIMIT):
            assert await rate_limit.check_alertmanager("198.51.100.1") is True
        assert await rate_limit.check_alertmanager("198.51.100.1") is False
        # Другой клиент не наказан за чужой флуд.
        assert await rate_limit.check_alertmanager("198.51.100.2") is True
        # Следующее окно — счётчик начинается заново.
        clock["t"] += rate_limit.WINDOW_SECONDS
        assert await rate_limit.check_alertmanager("198.51.100.1") is True


@pytest.mark.asyncio
async def test_degradation_is_observable_in_metrics():
    """Каждая недоступность Redis инкрементит метрики (иначе деградация невидима)."""
    errors_before = _metric(
        "rate_limit_redis_errors_total", {"limiter": "alertmanager"}
    )
    blocked_before = _metric(
        "rate_limit_local_fallback_total",
        {"limiter": "alertmanager", "decision": "blocked"},
    )
    allowed_before = _metric(
        "rate_limit_local_fallback_total",
        {"limiter": "alertmanager", "decision": "allowed"},
    )

    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        for _ in range(rate_limit.ALERTMANAGER_RATE_LIMIT + 2):
            await rate_limit.check_alertmanager("203.0.113.32")

    assert (
        _metric("rate_limit_redis_errors_total", {"limiter": "alertmanager"})
        - errors_before
        == rate_limit.ALERTMANAGER_RATE_LIMIT + 2
    )
    assert (
        _metric(
            "rate_limit_local_fallback_total",
            {"limiter": "alertmanager", "decision": "allowed"},
        )
        - allowed_before
        == rate_limit.ALERTMANAGER_RATE_LIMIT
    )
    assert (
        _metric(
            "rate_limit_local_fallback_total",
            {"limiter": "alertmanager", "decision": "blocked"},
        )
        - blocked_before
        == 2
    )


@pytest.mark.asyncio
async def test_redis_warning_is_throttled():
    """Warning логируется один раз в минуту, подавленные считаются."""
    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        with patch.object(rate_limit.log, "warning") as warn:
            for _ in range(5):
                await rate_limit.check_alertmanager("203.0.113.33")

    unavailable_logs = [
        c for c in warn.call_args_list if c.args and c.args[0] == "ratelimit.redis_unavailable"
    ]
    assert len(unavailable_logs) == 1
    assert rate_limit._redis_warn_state["suppressed"] == 4


@pytest.mark.asyncio
async def test_healthy_redis_path_unchanged():
    """Живой Redis: общий счётчик, in-process fallback не участвует."""
    with patch.object(rate_limit, "_get_client", return_value=_live_redis()):
        results = [
            await rate_limit.check_alertmanager("203.0.113.34") for _ in range(11)
        ]
    assert results[:10] == [True] * 10
    assert results[10] is False
    assert not rate_limit._fallback_counters


@pytest.mark.asyncio
async def test_empty_client_ip_still_passes():
    """Пустой ключ бакета (нет client.host) — поведение прежнее, пропускаем."""
    assert await rate_limit.check_alertmanager("") is True


@pytest.mark.asyncio
async def test_fallback_map_is_bounded(monkeypatch):
    """Флуд с разных IP не растит словарь без границы (утечка памяти в поде)."""
    monkeypatch.setattr(rate_limit, "_FALLBACK_MAX_KEYS", 16)
    with patch.object(rate_limit, "_get_client", return_value=_dead_redis()):
        for i in range(200):
            await rate_limit.check_alertmanager(f"192.0.2.{i % 200}")
    assert len(rate_limit._fallback_counters) <= 16
