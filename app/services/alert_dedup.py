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

Двухфазность: decide_send наращивает chronic-счётчик TENTATIVE-но — ДО
фактической отправки embed-а (иначе конкурентные batch-и теряют инкременты).
Вторая фаза на стороне caller-а: при недоставке он обязан позвать
`rollback_undelivered`, иначе подавление считает embed-ы, которых в канале
не было.
"""
from __future__ import annotations

import asyncio
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
# Сколько живёт маркер «право на resurface-embed уже выдано». Закрывает ровно
# окно гонки двух реплик, разбирающих ОДИН AM-batch (секунды), поэтому минуты,
# а не 6h: следующий legit-resurface через 2h+ тишины маркер глушить не должен.
RESURFACE_CLAIM_SECONDS = 120

_redis: Optional[aioredis.Redis] = None


def _get_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


# L2-state — ДВА ключа вместо прежнего JSON-blob-а:
#   *:cnt  — атомарный счётчик fires в окне. Создаётся через SET NX EX
#            (TTL ставится атомарно при создании и БОЛЬШЕ НЕ ОБНОВЛЯЕТСЯ —
#            окно якорится на первом fire), наращивается INCR-ом.
#            Прежний GET→modify→SET терял инкременты при конкурентных
#            batch-ах, а `SET ... ex=WINDOW` на каждом апдейте сдвигал TTL —
#            хроник, стреляющий чаще раза в 6h, не истекал НИКОГДА.
#   *:last — unix-ts последнего fire (для quiet-reset >2h). Его TTL можно
#            обновлять: он трекает именно «последний раз видели».
def _count_key(alertname: str, service: str) -> str:
    return f"enrich:lastsent:{alertname}:{service}:cnt"


def _last_key(alertname: str, service: str) -> str:
    return f"enrich:lastsent:{alertname}:{service}:last"


#   *:resurface — короткоживущий маркер «этот resurface уже кто-то забрал».
#            Ставится SET NX (атомарный single-winner, Lua не нужен —
#            одной команды достаточно). Без него ветка resurface была
#            GET→check→SET: две реплики, разобравшие один AM-batch после
#            >2h тишины, обе видели старый `last` и обе слали 🌀-embed.
def _resurface_key(alertname: str, service: str) -> str:
    return f"enrich:lastsent:{alertname}:{service}:resurface"


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

    ДВУХФАЗНОСТЬ: SEND / SEND_RESURFACED инкрементят chronic-счётчик
    TENTATIVE-но — ДО того, как embed реально ушёл в канал (иначе
    конкурентные batch-и теряли бы инкременты). Вторая фаза —
    подтверждение: caller ОБЯЗАН при недоставке позвать
    `rollback_undelivered(alertname, service, decision)`, иначе подавление
    считает embed-ы, которых в канале не было (см. её докстринг).

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
            # incidents_on — СИНХРОННЫЙ SQL, а зовут нас из async-хендлера
            # API-процесса: прямой вызов блокировал event loop (на storm-е
            # вместе с health-пробами). Уводим в thread pool, как
            # enrich_alert_async. Конкурентно с той же `db` не вызываем —
            # decide_send и enrich идут последовательно в одном хендлере,
            # так что сессией в каждый момент владеет один поток.
            recent = await asyncio.to_thread(
                incidents_on,
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
        key_cnt = _count_key(alertname, service)
        key_last = _last_key(alertname, service)
        now_unix = int(fire_at.timestamp())

        raw_last = await client.get(key_last)
        last: Optional[int] = None
        if raw_last is not None:
            try:
                last = int(raw_last)
            except (TypeError, ValueError):
                # Битый state — чистим и считаем первым fire-ом.
                await client.delete(key_last)
                await client.delete(key_cnt)

        if last is not None:
            delta = now_unix - last
            if delta > CHRONIC_QUIET_RESET_SECONDS:
                # >2h тишины — это resurface. Право на 🌀-embed выдаётся
                # АТОМАРНО (SET NX на маркер): прежний GET→check→SET
                # раздавал SEND_RESURFACED обеим репликам, разобравшим один
                # batch, — два одинаковых embed-а в канал.
                won = await client.set(
                    _resurface_key(alertname, service),
                    now_unix,
                    ex=RESURFACE_CLAIM_SECONDS,
                    nx=True,
                )
                if won:
                    # Сбрасываем counter и якорим новое окно на текущем fire.
                    await client.set(key_cnt, 1, ex=CHRONIC_WINDOW_SECONDS)
                    await client.set(key_last, now_unix, ex=CHRONIC_WINDOW_SECONDS)
                    log.info(
                        "dedup.resurfaced",
                        alertname=alertname, service=service,
                        quiet_seconds=delta,
                    )
                    return Decision.SEND_RESURFACED
                # Гонку проиграли: 🌀-embed уже отправляет победитель, он же
                # сбросил окно. Наш fire идём считать обычным in-window
                # путём (второй 🌀 в канал не уходит).
                log.info(
                    "dedup.resurface_claim_lost",
                    alertname=alertname, service=service,
                    quiet_seconds=delta,
                )
        else:
            delta = 0

        # Fire в окне (или первый). SET NX EX создаёт счётчик С TTL атомарно
        # (якорь = первый fire), INCR наращивает атомарно — конкурентные
        # batch-ы больше не теряют инкременты. TTL счётчика дальше НЕ
        # трогаем: через CHRONIC_WINDOW_SECONDS от первого fire ключ истечёт
        # и хроник всплывёт снова (документированное sliding-window поведение).
        await client.set(key_cnt, 0, ex=CHRONIC_WINDOW_SECONDS, nx=True)
        new_count = int(await client.incr(key_cnt))
        await client.set(key_last, now_unix, ex=CHRONIC_WINDOW_SECONDS)

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


async def rollback_undelivered(
    alertname: str,
    service: Optional[str],
    decision: Decision,
) -> None:
    """Фаза подтверждения: снять tentative-инкремент, если embed НЕ ушёл.

    Инцидентный контекст: enrich+send в /alertmanager/enrich-and-forward
    обёрнут в `except Exception → log.warning`, а счётчик наращивался ещё в
    decide_send. Три подряд неудачные доставки (Discord 5xx, таймаут,
    падение enrichment-а) поднимали счётчик до 3 — и четвёртый, уже
    успешный fire получал SUPPRESS_CHRONIC, хотя в канале не было НИ ОДНОГО
    embed-а. Дальше 6h-окно молчало целиком.

    Откатываем ровно то, что гейтит подавление:

      * счётчик — DECR; ноль/минус удаляем (информации не несёт, а DECR по
        уже истёкшему ключу создал бы его БЕЗ TTL — та же грабля, что была
        с cb:fail:* в resilience.py);
      * resurface-маркер — DEL, чтобы 🌀-embed можно было отправить снова.

    `last` НЕ откатываем намеренно: он значит «последний раз ВИДЕЛИ fire», а
    fire реально был; от него зависит только quiet-reset.

    Fail-open: сбой Redis тут ничего не ломает — остаётся прежнее (худшее)
    поведение с завышенным счётчиком.
    """
    if not service or decision not in (Decision.SEND, Decision.SEND_RESURFACED):
        return
    try:
        client = _get_client()
        key_cnt = _count_key(alertname, service)
        new_count = int(await client.decr(key_cnt))
        if new_count <= 0:
            await client.delete(key_cnt)
        if decision == Decision.SEND_RESURFACED:
            await client.delete(_resurface_key(alertname, service))
        log.info(
            "dedup.rollback_undelivered",
            alertname=alertname, service=service,
            decision=decision.value, count=max(new_count, 0),
        )
    except Exception as e:
        log.warning("dedup.rollback_failed", error=str(e))


async def close() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
