"""Межпрогонное состояние дайджеста: снапшоты дня и heartbeat beat-тасков.

Вынесено из stats_digest по тому же шву, что доставка отчёта из
IncidentPipeline: это не построение дайджеста, а то, что живёт МЕЖДУ его
прогонами. Секции меняются при каждой правке отчёта, а ключи и TTL —
контракт с Redis, переживающий перезапуск воркера; держать их рядом означало
править один файл по двум несвязанным поводам.

Всё здесь fail-open: Redis — канал наблюдаемости, его недоступность обязана
деградировать в «нет вчерашних данных» (и `(new baseline)` в тексте), а не
ронять дайджест или beat-таск.

Два клиента, и это намеренно:
  * async (`alert_dedup._get_client`) — снапшоты, читаются из async-сборки;
  * sync (`_get_beat_redis`) — heartbeat, пишется celery-сигналом
    `task_postrun` в процессе без event-loop.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import structlog

from app.config import settings

log = structlog.get_logger()

__all__ = [
    "DAY_SNAPSHOT_REDIS_KEY",
    "TOPOLOGY_SNAPSHOT_REDIS_KEY",
    "DAY_SNAPSHOT_REDIS_TTL",
    "FIRING_SERIES_REDIS_KEY",
    "FIRING_SERIES_REDIS_TTL",
    "FIRING_SERIES_WINDOW_MINUTES",
    "FIRING_SERIES_WINDOW_LABEL",
    "BEAT_HEARTBEAT_REDIS_PREFIX",
    "BEAT_HEARTBEAT_REDIS_TTL",
    "read_last_firing_series",
    "write_last_firing_series",
    "read_day_snapshot",
    "write_day_snapshot",
    "read_topology_snapshot",
    "write_topology_snapshot",
    "detect_first_run",
    "beat_heartbeat_key",
    "record_task_heartbeat",
    "get_beat_last_run",
]

# Все Redis-ключи под prefix `stats:`. TTL не больше 25h — переписывается
# каждым daily-run; при пропуске следующий run честно скажет «(new baseline)»
# вместо того, чтобы сравнивать с позавчерашним днём как со вчерашним.
DAY_SNAPSHOT_REDIS_KEY = "stats:digest:last_day_snapshot"
TOPOLOGY_SNAPSHOT_REDIS_KEY = "stats:topology:last_day_snapshot"
DAY_SNAPSHOT_REDIS_TTL = 25 * 3600

# Firing-series day-over-day trend.
FIRING_SERIES_REDIS_KEY = "stats:firing_series:last_day"
FIRING_SERIES_REDIS_TTL = 25 * 3600

# Окно, за которое реально берётся `fired_series` (`ALERTS{alertstate="firing"}`
# за последние 5 минут). Секции, считающие доли по этому списку, обязаны
# подписывать в заголовке ЭТО окно, а не «24h»: «Noisemakers (24h)» поверх
# пятиминутного снимка — ложное обобщение.
FIRING_SERIES_WINDOW_MINUTES = 5
FIRING_SERIES_WINDOW_LABEL = f"снимок firing-серий за {FIRING_SERIES_WINDOW_MINUTES}m"

# Beat-task heartbeat. Пишется `task_postrun`-сигналом из app/workers/tasks.py
# для каждого таска из `BEAT_HEARTBEAT_TASKS`; `pipeline_health_section`
# отличает по нему «task ходит, но данные stale» (VM scrape gap) от «task
# завис». TTL 7 дней — на случай долгого простоя.
BEAT_HEARTBEAT_REDIS_PREFIX = "stats:beat:last_run"
BEAT_HEARTBEAT_REDIS_TTL = 7 * 24 * 3600


# --- Async-слой: снапшоты предыдущего дня ---------------------------------


async def _async_client():
    """Async Redis-клиент дедупа — единственный на процесс, переиспользуем."""
    from app.services.alert_dedup import _get_client
    return _get_client()


async def read_last_firing_series() -> Optional[int]:
    """Вчерашний firing-count. None если ключа нет (или Redis недоступен)."""
    try:
        client = await _async_client()
        raw = await client.get(FIRING_SERIES_REDIS_KEY)
        if raw is None:
            return None
        return int(raw)
    except Exception as e:
        log.warning("stats_digest.firing_series_redis_read_failed", error=str(e))
        return None


async def write_last_firing_series(value: int) -> None:
    """Сохранить сегодняшний count для завтрашнего сравнения."""
    try:
        client = await _async_client()
        await client.set(FIRING_SERIES_REDIS_KEY, str(value), ex=FIRING_SERIES_REDIS_TTL)
    except Exception as e:
        log.warning("stats_digest.firing_series_redis_write_failed", error=str(e))


async def read_day_snapshot() -> Optional[Dict[str, Any]]:
    """Вчерашний snapshot. None если ключа нет / битый JSON."""
    try:
        client = await _async_client()
        raw = await client.get(DAY_SNAPSHOT_REDIS_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("stats_digest.snapshot_read_failed", error=str(e))
        return None


async def write_day_snapshot(snapshot: Dict[str, Any]) -> None:
    """Сохранить сегодняшний snapshot для завтрашнего Δ-сравнения."""
    try:
        client = await _async_client()
        await client.set(
            DAY_SNAPSHOT_REDIS_KEY,
            json.dumps(snapshot, default=str),
            ex=DAY_SNAPSHOT_REDIS_TTL,
        )
    except Exception as e:
        log.warning("stats_digest.snapshot_write_failed", error=str(e))


async def read_topology_snapshot() -> Optional[Dict[str, Any]]:
    """Topology snapshot предыдущего дня. None если ключа нет."""
    try:
        client = await _async_client()
        raw = await client.get(TOPOLOGY_SNAPSHOT_REDIS_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("stats_digest.topology_snapshot_read_failed", error=str(e))
        return None


async def write_topology_snapshot(snapshot: Dict[str, Any]) -> None:
    try:
        client = await _async_client()
        await client.set(
            TOPOLOGY_SNAPSHOT_REDIS_KEY,
            json.dumps(snapshot, default=str),
            ex=DAY_SNAPSHOT_REDIS_TTL,
        )
    except Exception as e:
        log.warning("stats_digest.topology_snapshot_write_failed", error=str(e))


def detect_first_run(snapshot: Optional[Dict[str, Any]]) -> bool:
    """First-run = снапшота нет либо он пуст.

    Отличать первый запуск от «вчера было ноль» обязательно: иначе дайджест
    рисует эффектную дельту там, где сравнивать было не с чем.
    """
    if snapshot is None:
        return True
    if not snapshot:
        return True
    return False


# --- Sync-слой: heartbeat beat-тасков -------------------------------------


def beat_heartbeat_key(task_name: str) -> str:
    return f"{BEAT_HEARTBEAT_REDIS_PREFIX}:{task_name}"


# Module-level sync-клиент. Раньше КАЖДЫЙ вызов record/get строил новый
# redis.Redis.from_url и не закрывал его — постоянный connection churn
# (heartbeat пишется сигналом каждого beat-таска). redis-py держит пул
# внутри: он потокобезопасен, переживает fork (пул сбрасывается по pid-check)
# и сам переподключается — одного клиента на процесс достаточно.
#
# Кэш ключуется ИДЕНТИЧНОСТЬЮ модуля `redis`: тесты подменяют его через
# sys.modules, и клиент от прежнего модуля не должен пережить подмену.
_beat_redis_cache: Optional[Tuple[Any, Any]] = None  # (redis-модуль, клиент)


def _get_beat_redis():
    """Переиспользуемый sync-клиент для heartbeat-ключей.

    Может бросить (Redis недоступен на этапе конструирования) — вызывающие
    оборачивают в свой try/except (fail-open). Неудачная инициализация НЕ
    кэшируется.
    """
    global _beat_redis_cache
    import redis
    if _beat_redis_cache is not None and _beat_redis_cache[0] is redis:
        return _beat_redis_cache[1]
    client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    _beat_redis_cache = (redis, client)
    return client


def record_task_heartbeat(task_name: str, ts: Optional[datetime] = None) -> None:
    """Зафиксировать успешное завершение beat-таска.

    Sync: вызывается из celery `task_postrun`, где event-loop нет. Идемпотентна
    — пишет ISO-timestamp с TTL 7d. Fail-open: monitoring-канал не должен
    ронять сам таск.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)
    try:
        client = _get_beat_redis()
        client.set(
            beat_heartbeat_key(task_name),
            ts.isoformat(),
            ex=BEAT_HEARTBEAT_REDIS_TTL,
        )
    except Exception as e:
        log.warning(
            "stats_digest.beat_heartbeat_write_failed",
            task=task_name,
            error=str(e),
        )


def get_beat_last_run(task_name: str) -> Optional[datetime]:
    """last_run beat-таска. None если ключа нет (таск ещё не отработал)."""
    try:
        client = _get_beat_redis()
        raw = client.get(beat_heartbeat_key(task_name))
        if raw is None:
            return None
        # decode_responses=True уже даёт str.
        return datetime.fromisoformat(raw)
    except Exception as e:
        log.warning(
            "stats_digest.beat_heartbeat_read_failed",
            task=task_name,
            error=str(e),
        )
        return None
