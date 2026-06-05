"""Sync per-service метрик из VictoriaMetrics → kg_service_health.

Beat-task `kg_metrics_sync` каждые ~10 мин:
1. Берём все real (synthetic=False) services из KG.
2. Для каждого делаем 5 PromQL-запросов (cpu/mem/restarts/5xx/p95) через
   VMClient.query_instant.
3. Если хоть одна метрика > 0 — пишем строку. Полностью NULL-only ряды
   не вставляем (значит метрик нет — экспортёр не покрывает сервис).

Идемпотентность: UNIQUE(service_id, ts); commit чанками. Все exceptions
ловятся per-service — один проблемный сервис не валит весь sync.

Параллелизм (recon 2026-05-25): fetch-фаза идёт через `asyncio.gather`
с `asyncio.Semaphore(KG_METRICS_SYNC_CONCURRENCY)` — раньше sequential
loop по 2908 services × 5 PromQL не укладывался в 10-минутный cron, и
kg_anomaly_detection_task репортил `skipped_no_current=8020`. Запись в
БД остаётся серийной после gather: SQLAlchemy Session не thread-safe.

CLI: `python -m app.knowledge_graph.metrics_sync`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.schema import Service, ServiceHealth

log = logging.getLogger(__name__)


# ── PromQL шаблоны per service ─────────────────────────────────────────────
# Все запросы по (namespace, pod) — pod prefix матчит имя deployment.
# `container!=""` отсеивает pause-контейнер. Окна 5m — RecordingRule в VM
# уже считает rate, нам нужен short-window average.

def _q_cpu_pct(namespace: str, deployment: str) -> str:
    return (
        f'avg(rate(container_cpu_usage_seconds_total'
        f'{{namespace="{namespace}",pod=~"{deployment}-.*",container!=""}}[5m]))'
        f' * 100'
    )


def _q_mem_pct(namespace: str, deployment: str) -> str:
    # working_set / (limit OR request).
    # Тонкости (recon 2026-05-22):
    #  1. cAdvisor отдаёт несколько ts с одинаковыми (namespace,pod,container)
    #     labels (per-image, per-sandbox) → many-to-one join даёт 0 rows
    #     без агрегата. Решение: `avg by(namespace,pod,container)` слева.
    #  2. У большинства WO pods отсутствует memory limit → division by NULL.
    #     Fallback на request через `or on(...) (request)`.
    base = (
        f'avg by(namespace,pod,container) (container_memory_working_set_bytes'
        f'{{namespace="{namespace}",pod=~"{deployment}-.*",container!=""}})'
    )
    limit = (
        f'kube_pod_container_resource_limits'
        f'{{namespace="{namespace}",pod=~"{deployment}-.*",resource="memory"}}'
    )
    request = (
        f'kube_pod_container_resource_requests'
        f'{{namespace="{namespace}",pod=~"{deployment}-.*",resource="memory"}}'
    )
    return (
        f'avg({base} / on(namespace, pod, container) '
        f'(({limit}) or on(namespace, pod, container) ({request}))) * 100'
    )


def _q_restarts_rate(namespace: str, deployment: str) -> str:
    # restarts/min за окно 15m → удобнее в дашборд.
    return (
        f'sum(rate(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}",pod=~"{deployment}-.*"}}[15m])) * 60'
    )


def _q_http_5xx_rate(namespace: str, deployment: str) -> str:
    # ВАЖНО (recon 2026-05-22): prod WO API сервисы (60 namespace) НЕ
    # скрейпятся центральной VictoriaMetrics — `microsoft_aspnetcore_*` и
    # `gr_wo_*` существуют только в namespace=monitoring. serviceMonitor
    # / scrape config не покрывает prod-WO. Этот PromQL гарантированно
    # вернёт 0 в текущей конфигурации.
    # Когда scrape config поправят — раскомментировать ASP.NET Core
    # вариант ниже. До тех пор оставлен legacy http_requests_total для
    # cross-cluster совместимости.
    return (
        f'sum(rate(http_requests_total'
        f'{{namespace="{namespace}",service=~"{deployment}.*",status=~"5.."}}[5m]))'
    )
    # TODO когда WO API получит scrape config:
    # return (f'sum(rate(microsoft_aspnetcore_hosting_failed_requests'
    #         f'{{namespace="{namespace}",pod=~"{deployment}-.*"}}[5m]))')


def _q_p95_latency_ms(namespace: str, deployment: str) -> str:
    # См. комментарий выше — prod WO не скрейпится. Запрос валидный,
    # но в текущем кластере вернёт 0.
    return (
        f'1000 * histogram_quantile(0.95, sum by(le)('
        f'rate(http_request_duration_seconds_bucket'
        f'{{namespace="{namespace}",service=~"{deployment}.*"}}[5m])'
        f'))'
    )
    # TODO когда WO API получит scrape config:
    # return (f'1000 * histogram_quantile(0.95, sum by(le)('
    #         f'rate(microsoft_aspnetcore_hosting_http_server_request_duration_bucket'
    #         f'{{namespace="{namespace}",pod=~"{deployment}-.*"}}[5m])))')


async def _fetch_service_metrics(
    vm: VMClient, namespace: str, name: str,
) -> Dict[str, Optional[float]]:
    """Параллельно собирает 5 метрик для одного сервиса."""
    queries = {
        "cpu_pct": _q_cpu_pct(namespace, name),
        "mem_pct": _q_mem_pct(namespace, name),
        "restarts_rate": _q_restarts_rate(namespace, name),
        "http_5xx_rate": _q_http_5xx_rate(namespace, name),
        "p95_latency_ms": _q_p95_latency_ms(namespace, name),
    }
    keys = list(queries.keys())
    values = await asyncio.gather(
        *[vm.query_instant(q) for q in queries.values()],
        return_exceptions=True,
    )
    out: Dict[str, Optional[float]] = {}
    for k, v in zip(keys, values):
        if isinstance(v, BaseException):
            out[k] = None
            continue
        # VMClient.query_instant возвращает 0.0 для пустого/ошибочного — мы
        # пишем 0.0 как валидную точку. None используется только при
        # gather-exception на этом ключе.
        out[k] = float(v)
    return out


def _has_any_signal(metrics: Dict[str, Optional[float]]) -> bool:
    """True если хоть одна метрика > 0 (значит экспортёр накрывает сервис)."""
    return any(
        (v is not None and v > 0.0) for v in metrics.values()
    )


def _insert_idempotent(
    db: Session,
    service_id: int,
    ts: datetime,
    metrics: Dict[str, Optional[float]],
    source: str,
) -> bool:
    """INSERT с защитой по UNIQUE(service_id, ts).

    Кроссдиалектно: ловим IntegrityError и rollback'аем nested savepoint.
    Возвращает True если строка реально вставилась.
    """
    row = ServiceHealth(
        service_id=service_id,
        ts=ts,
        cpu_pct=metrics.get("cpu_pct"),
        mem_pct=metrics.get("mem_pct"),
        restarts_rate=metrics.get("restarts_rate"),
        http_5xx_rate=metrics.get("http_5xx_rate"),
        p95_latency_ms=metrics.get("p95_latency_ms"),
        source=source,
    )
    try:
        with db.begin_nested():
            db.add(row)
        return True
    except IntegrityError:
        return False


async def _fetch_with_semaphore(
    sem: asyncio.Semaphore,
    vm: VMClient,
    svc_id: int,
    namespace: str,
    name: str,
) -> Tuple[int, str, str, Optional[Dict[str, Optional[float]]], Optional[BaseException]]:
    """Per-service wrapper: ждёт slot в semaphore, ловит исключения.

    Возвращает tuple `(svc_id, namespace, name, metrics_or_None, exc_or_None)`.
    Исключение в одном сервисе НЕ роняет gather — оно сериализуется в результат
    и обрабатывается в основном цикле.
    """
    async with sem:
        try:
            metrics = await _fetch_service_metrics(vm, namespace, name)
            return (svc_id, namespace, name, metrics, None)
        except BaseException as e:  # noqa: BLE001 — фиксируем всё, классифицируем выше
            return (svc_id, namespace, name, None, e)


async def _sync_service_health_async(db: Session) -> Dict[str, Any]:
    if not settings.VICTORIA_METRICS_URL:
        log.info("metrics_sync.skipped reason=no_vm_url")
        return {"skipped": "no_vm_url"}

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
    services: List[Service] = (
        db.query(Service).filter(Service.synthetic.is_(False)).all()
    )
    ts = datetime.utcnow()

    concurrency = max(1, int(settings.KG_METRICS_SYNC_CONCURRENCY))
    stats: Dict[str, Any] = {
        "real_services": len(services),
        "concurrency": concurrency,
        "fetched": 0,
        "with_signal": 0,
        "inserted": 0,
        "skipped_empty": 0,
        "skipped_dup": 0,
        "errors": 0,
        "duration_ms": 0,
    }

    if not services:
        log.info("metrics_sync.done real=0 (no real services in KG)")
        return stats

    # ── Fetch + write, чередуя через as_completed ──────────────────────────
    # SQLAlchemy Session не thread/async-safe → запись делаем серийно в этом
    # же event-loop по мере готовности фетчей. Per-service exception ловится
    # в _fetch_with_semaphore и в виде тапла `(..., None, exc)` попадает в
    # результат — итерация не падает.
    #
    # КРИТИЧНО (recon 2026-06-05): раньше был один `db.commit()` в самом конце
    # после полного gather. При росте до ~2.4k сервисов полный проход стал
    # вылезать за soft-time-limit (а наложение прогонов перегружало одиночный
    # vmsingle) → задача убивалась ДО commit → kg_service_health/anomaly
    # замёрзли с 2026-06-01. Теперь коммитим батчами по мере готовности:
    # убитый/затянувшийся прогон всё равно персистит всё, что успел собрать.
    sem = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()
    coros = [
        _fetch_with_semaphore(
            sem, vm, cast(int, s.id), cast(str, s.namespace), cast(str, s.name),
        )
        for s in services
    ]

    COMMIT_BATCH = 250
    since_commit = 0
    for fut in asyncio.as_completed(coros):
        svc_id, namespace, name, metrics, exc = await fut
        if exc is not None or metrics is None:
            stats["errors"] += 1
            log.warning(
                "metrics_sync.fetch_failed ns=%s name=%s err=%s",
                namespace, name, exc,
            )
            continue

        stats["fetched"] += 1
        if not _has_any_signal(metrics):
            stats["skipped_empty"] += 1
            continue

        stats["with_signal"] += 1
        inserted = _insert_idempotent(
            db, service_id=svc_id, ts=ts, metrics=metrics, source="vm",
        )
        if inserted:
            stats["inserted"] += 1
        else:
            stats["skipped_dup"] += 1

        since_commit += 1
        if since_commit >= COMMIT_BATCH:
            db.commit()
            since_commit = 0

    db.commit()
    fetch_elapsed = time.monotonic() - t0
    stats["duration_ms"] = int((time.monotonic() - t0) * 1000)

    log.info(
        "metrics_sync.done real=%d concurrency=%d fetched=%d with_signal=%d "
        "inserted=%d skipped_empty=%d skipped_dup=%d errors=%d "
        "fetch_ms=%d total_ms=%d",
        stats["real_services"], concurrency, stats["fetched"], stats["with_signal"],
        stats["inserted"], stats["skipped_empty"], stats["skipped_dup"],
        stats["errors"], int(fetch_elapsed * 1000), stats["duration_ms"],
    )
    return stats


def sync_service_health(db: Session) -> Dict[str, Any]:
    """Sync-обёртка для Celery (sync-context). Внутри — asyncio.run на VM-клиент."""
    return asyncio.run(_sync_service_health_async(db))


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(sync_service_health(db))
    finally:
        db.close()
