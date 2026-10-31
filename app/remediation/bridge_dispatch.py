"""Постановка детерминированного моста в Celery из процесса API.

Инцидент 31.10.2026: мост (`bridge.run_bridge_for_incident`) запускался
`asyncio.create_task` прямо в event loop API на КАЖДЫЙ алерт батча
`/store`. Внутри — синхронные `SessionLocal()`/SQL, `enrich_alert_async`,
K8sFacts (kube API) и dry-run: 13 сквадов в crashloop дали десятки задач,
каждая со своим соединением к БД, loop встал, оба пода `sre-ai-api` не
отвечали на пробы, Alertmanager терял вебхуки.

Здесь — только то, что дёшево и не блокирует loop:
  * `wants_bridge(labels)` — флаг + alertname в реестре (кэш в памяти);
  * дедуп постановки: Redis `SET NX EX` по (alertname, namespace,
    pod/workload) на REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS, плюс
    дедуп внутри батча — батч на 30 алертов одного пода даёт одну задачу;
  * `apply_async` задачи `remediation_bridge` (очередь `celery`) в потоке.

Вся работа моста — в воркере. Дедуп предложений (playbook, uid,
incarnation, 6 ч) остаётся внутри задачи (`bridge.claim_proposal`).

Redis недоступен — задачу НЕ ставим (fail-closed): брокер Celery — тот же
Redis, а мост необязателен; лучше пропустить предложение, чем в шторм
висеть на постановке.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Set

import structlog

from app.config import settings
from app.remediation.bridge import wants_bridge

if TYPE_CHECKING:
    from app.models.incident import Incident

log = structlog.get_logger()

__all__ = ["enqueue_bridge", "enqueue_dedup_key"]

_ENQUEUE_PREFIX = "copilot:remediation_bridge:enqueue:"
_REDIS_TIMEOUT_SEC = 2.0

_client: Any = None


def _ttl_seconds() -> int:
    try:
        return max(1, int(getattr(settings, "REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS", 600)))
    except (TypeError, ValueError):
        return 600


def enqueue_dedup_key(labels: Optional[Dict[str, Any]], namespace: Optional[str] = None) -> str:
    """(alertname, namespace, pod/workload) → короткий хэш.

    Под точнее workload-а: два пода одного Deployment-а в crashloop — один
    и тот же uid цели, их схлопнет дедуп предложений в задаче; здесь же
    отсекаем повторы ОДНОГО алерта (AM пере-шлёт каждые group_interval).
    """
    lb = labels or {}
    target = (
        lb.get("pod") or lb.get("deployment") or lb.get("statefulset")
        or lb.get("daemonset") or lb.get("job_name") or lb.get("service") or "-"
    )
    raw = f"{lb.get('alertname') or '-'}|{namespace or lb.get('namespace') or '-'}|{target}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _redis() -> Any:
    """Async-клиент Redis (lazy). Синхронный redis-py в API блокировал бы loop."""
    global _client
    if _client is None:
        from redis import asyncio as aioredis
        _client = aioredis.from_url(
            settings.REDIS_URL,
            socket_timeout=_REDIS_TIMEOUT_SEC,
            socket_connect_timeout=_REDIS_TIMEOUT_SEC,
        )
    return _client


async def _claim(key: str) -> bool:
    """True — ключ наш, ставим задачу. Ошибка Redis — False (fail-closed)."""
    try:
        ok = await asyncio.wait_for(
            _redis().set(_ENQUEUE_PREFIX + key, "1", nx=True, ex=_ttl_seconds()),
            timeout=_REDIS_TIMEOUT_SEC,
        )
        return bool(ok)
    except Exception as e:
        log.warning("remediation_bridge.enqueue_claim_failed", error=type(e).__name__)
        return False


async def _release(key: str) -> None:
    try:
        await asyncio.wait_for(_redis().delete(_ENQUEUE_PREFIX + key), timeout=_REDIS_TIMEOUT_SEC)
    except Exception as e:
        log.debug("remediation_bridge.enqueue_release_failed", error=type(e).__name__)


def _apply_async(incident_data: Dict[str, Any], card_key: Optional[str]) -> None:
    from app.workers.tasks import remediation_bridge_task
    remediation_bridge_task.apply_async(
        args=[incident_data], kwargs={"card_key": card_key},
    )


async def enqueue_bridge(
    incidents: Iterable["Incident"], *, card_key: Optional[str] = None,
) -> int:
    """Поставить мост в Celery для подходящих алертов. Возвращает число задач.

    Наружу не бросает: мост — необязательная надстройка, ответ
    Alertmanager-у от него не зависит.
    """
    seen: Set[str] = set()
    queued = 0
    for incident in incidents:
        try:
            if incident.status == "resolved" or not wants_bridge(incident.labels):
                continue
            key = enqueue_dedup_key(incident.labels, incident.namespace)
            if key in seen:
                continue
            seen.add(key)
            if not await _claim(key):
                continue
            try:
                # apply_async — синхронный publish в брокер: в поток, чтобы
                # медленный Redis не останавливал loop.
                await asyncio.to_thread(
                    _apply_async, incident.model_dump(mode="json"), card_key,
                )
            except Exception as e:
                await _release(key)
                log.warning("remediation_bridge.enqueue_failed", error=type(e).__name__)
                continue
            queued += 1
        except Exception as e:
            log.warning("remediation_bridge.enqueue_error", error=type(e).__name__)
    if queued:
        log.info("remediation_bridge.enqueued", tasks=queued, card=bool(card_key))
    return queued
