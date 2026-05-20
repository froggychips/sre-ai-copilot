"""Дедупликация Discord-алертов: chronic-suppress + rollout-noise-silent.

Слои:

  L2 (chronic suppress) — Redis-state per (alertname, service).
      Если ≥3 повтора за 6h-окно И last_fire <2h назад → SUPPRESS_CHRONIC,
      не шлём embed (только в KG store). Quiet >2h → SEND_RESURFACED.

  L4 (rollout-noise silent) — для KubeDeploymentGenerationMismatch /
      KubeReplicaSetMismatch проверяем: предыдущий fire того же ключа
      резолвнулся за <10 мин? → silent, считаем что это rollout transient,
      не настоящий incident.

Fail-open: при недоступности Redis возвращаем SEND (как в rate_limit.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import structlog
from redis import asyncio as aioredis
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.queries import incidents_on

log = structlog.get_logger()

# Окно «хронической» проверки и quiet-reset.
CHRONIC_WINDOW_SECONDS = 6 * 3600       # 6h хранения state
CHRONIC_QUIET_RESET_SECONDS = 2 * 3600  # >2h тишины → resurfaced
CHRONIC_MIN_COUNT = 3                   # с какого N считаем хроникой
ROLLOUT_NOISE_THRESHOLD_SECONDS = 10 * 60  # <10 мин длительность = noise

_redis: Optional[aioredis.Redis] = None


def _get_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _key(alertname: str, service: str) -> str:
    return f"enrich:lastsent:{alertname}:{service}"


class Decision(str, Enum):
    """Что делать с алертом на уровне Discord-send."""

    SEND = "send"                          # обычный fire — embed уходит
    SEND_RESURFACED = "send_resurfaced"    # вернулся после quiet >2h
    SUPPRESS_CHRONIC = "suppress_chronic"  # уже >=3 за 6h, last <2h назад
    SUPPRESS_ROLLOUT = "suppress_rollout"  # rollout transient, не настоящий
    SEND_NO_DEDUP = "send_no_dedup"        # service пустой / fail-open


async def decide_send(
    alertname: str,
    namespace: Optional[str],
    service: Optional[str],
    severity: str,
    db: Session,
    fire_at: Optional[datetime] = None,
) -> Decision:
    """Главная точка дедупа — вызывается из enrich-and-forward.

    Args:
        alertname: e.g. KubePodCrashLooping
        namespace: k8s namespace (нужен для L4 lookup в kg_alerts)
        service: deployment/service name из labels.service
        severity: critical/warning/info (для будущего бранчинга)
        db: SQLAlchemy session — нужно для L4 проверки в kg_alerts
        fire_at: timestamp текущего fire-а (default now-UTC)
    """
    if not service:
        # Без service-id ключ не построить — fail-open.
        return Decision.SEND_NO_DEDUP

    fire_at = fire_at or datetime.now(timezone.utc)

    # ── L4: rollout-noise silent для mismatch-class alerts ─────────────
    if alertname in {"KubeDeploymentGenerationMismatch", "KubeReplicaSetMismatch"} and namespace:
        try:
            recent = incidents_on(
                db,
                namespace=namespace,
                service_name=service,
                since=fire_at - timedelta(hours=2),
                until=fire_at,
            )
            # Достаточно ОДНОГО предыдущего fire где resolved_at - fired_at < 10m,
            # чтобы счесть текущий повтор за rollout-noise.
            for ev in recent:
                ra = ev.get("resolved_at")
                fa = ev.get("fired_at")
                if ra is None or fa is None:
                    continue
                duration = (ra - fa).total_seconds()
                if 0 < duration < ROLLOUT_NOISE_THRESHOLD_SECONDS:
                    log.info(
                        "dedup.rollout_silent",
                        alertname=alertname,
                        service=service,
                        prev_duration_s=int(duration),
                    )
                    return Decision.SUPPRESS_ROLLOUT
        except Exception as e:
            log.warning("dedup.rollout_check_failed", error=str(e))

    # ── L2: chronic suppress через Redis state ─────────────────────────
    try:
        client = _get_client()
        key = _key(alertname, service)
        raw = await client.get(key)
        now_unix = int(fire_at.timestamp())

        if raw is None:
            # Первый fire в окне — записываем state и шлём.
            state = {
                "first": now_unix,
                "last": now_unix,
                "count": 1,
            }
            await client.set(key, json.dumps(state), ex=CHRONIC_WINDOW_SECONDS)
            return Decision.SEND

        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            await client.delete(key)
            return Decision.SEND

        last = int(state.get("last") or 0)
        count = int(state.get("count") or 0)
        delta = now_unix - last

        if delta > CHRONIC_QUIET_RESET_SECONDS:
            # >2h тишины — это resurface, сбрасываем counter.
            state = {"first": now_unix, "last": now_unix, "count": 1}
            await client.set(key, json.dumps(state), ex=CHRONIC_WINDOW_SECONDS)
            log.info(
                "dedup.resurfaced",
                alertname=alertname, service=service,
                quiet_seconds=delta,
            )
            return Decision.SEND_RESURFACED

        # В окне <2h — увеличиваем counter.
        new_count = count + 1
        state["last"] = now_unix
        state["count"] = new_count
        # TTL не обновляем — sliding window управляется first; ключ сам
        # уйдёт через CHRONIC_WINDOW_SECONDS от первого fire-а.
        await client.set(key, json.dumps(state), ex=CHRONIC_WINDOW_SECONDS)

        if new_count >= CHRONIC_MIN_COUNT:
            log.info(
                "dedup.chronic_suppress",
                alertname=alertname, service=service,
                count=new_count, last_delta_s=delta,
            )
            return Decision.SUPPRESS_CHRONIC

        return Decision.SEND
    except Exception as e:
        log.warning("dedup.redis_unavailable", error=str(e))
        return Decision.SEND_NO_DEDUP


async def close() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
