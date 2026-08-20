"""Окно подавления повторов, общее для всех форков воркера.

Задачи, которые сами решают «об этом уже сообщали, не шлём снова», держали
это решение в словаре модуля. В prefork-воркере такой словарь принадлежит
одному форку, и подавление работает ровно настолько, насколько живёт форк:

  * `--concurrency=4` — четыре независимых копии состояния. Один и тот же
    fail, попавший в разные форки, уезжает в Discord до четырёх раз;
  * `worker_max_memory_per_child=350MB` и `max_tasks_per_child` перезапускают
    форки постоянно (эти пороги выставлены после 14 OOMKill за двое суток),
    и каждый recycle обнуляет окно. Шестичасовое подавление на практике
    жило десятки минут.

Тот же класс проблемы решён в `kubectl_breaker` переносом состояния в Redis;
здесь ровно то же средство.

Fail-open по отношению к Redis: нет хранилища — считаем, что не подавляли, и
отправляем. Подавление шума не должно превращаться в потерю сигнала, и
именно такой компромисс был выбран, когда состояние держали в памяти
(«лучше один лишний embed, чем пропустить регрессию»).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

__all__ = ["should_fire", "mark_fired", "clear"]

_PREFIX = "copilot:fire_dedup:"


def _redis():
    """Клиент Redis или None. Недоступность Redis не должна ломать задачу."""
    try:
        import redis

        from app.config import settings
        return redis.from_url(settings.REDIS_URL, socket_timeout=2)
    except Exception as e:  # noqa: BLE001
        log.debug("fire_dedup.redis_unavailable: %s", e)
        return None


def should_fire(channel: str, fingerprint: str) -> bool:
    """True — отправлять; False — про это уже сообщали внутри окна.

    `channel` разделяет счётчики разных задач: одинаковый fingerprint у
    self-health и stuck-alerts не должен глушить друг друга.
    """
    r = _redis()
    if r is None:
        return True
    try:
        return not r.exists(_PREFIX + channel + ":" + fingerprint)
    except Exception as e:  # noqa: BLE001
        log.debug("fire_dedup.check_failed: %s", e)
        return True


def mark_fired(channel: str, fingerprint: str, window_seconds: int) -> None:
    """Запомнить отправку на `window_seconds`.

    Ключ с TTL, а не таймстемп: истечение окна тогда — забота Redis, и
    сравнивать время не приходится вовсе. Заодно ключи не копятся: набор
    fingerprint'ов со временем меняется, и без TTL они оставались бы навсегда.
    """
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_PREFIX + channel + ":" + fingerprint, window_seconds, "1")
    except Exception as e:  # noqa: BLE001
        log.debug("fire_dedup.mark_failed: %s", e)


def clear(channel: str, fingerprint: Optional[str] = None) -> None:
    """Снять подавление. Без fingerprint — по всему каналу.

    Нужно, когда состояние починили и ждать конца окна незачем: следующий
    отчёт должен уйти сразу.
    """
    r = _redis()
    if r is None:
        return
    try:
        if fingerprint is not None:
            r.delete(_PREFIX + channel + ":" + fingerprint)
            return
        for key in r.scan_iter(match=_PREFIX + channel + ":*", count=100):
            r.delete(key)
    except Exception as e:  # noqa: BLE001
        log.debug("fire_dedup.clear_failed: %s", e)
