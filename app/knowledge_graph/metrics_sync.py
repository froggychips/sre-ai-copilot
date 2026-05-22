"""Sync per-service метрик из VictoriaMetrics → kg_service_health.

Beat-task `kg_metrics_sync` каждые ~10 мин:
1. Берём все real (synthetic=False) services из KG.
2. Для каждого делаем 5 PromQL-запросов (cpu/mem/restarts/5xx/p95) через
   VMClient.query_instant.
3. Если хоть одна метрика > 0 — пишем строку. Полностью NULL-only ряды
   не вставляем (значит метрик нет — экспортёр не покрывает сервис).

Идемпотентность: UNIQUE(service_id, ts); commit чанками. Все exceptions
ловятся per-service — один проблемный сервис не валит весь sync.

CLI: `python -m app.knowledge_graph.metrics_sync`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    # working_set / limit. Если limit=0 — fallback на 0.
    return (
        f'avg('
        f'  container_memory_working_set_bytes'
        f'  {{namespace="{namespace}",pod=~"{deployment}-.*",container!=""}}'
        f'  / on(namespace, pod, container)'
        f'  kube_pod_container_resource_limits'
        f'  {{namespace="{namespace}",pod=~"{deployment}-.*",resource="memory"}}'
        f') * 100'
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


async def _sync_service_health_async(db: Session) -> Dict[str, Any]:
    if not settings.VICTORIA_METRICS_URL:
        log.info("metrics_sync.skipped reason=no_vm_url")
        return {"skipped": "no_vm_url"}

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
    services: List[Service] = (
        db.query(Service).filter(Service.synthetic.is_(False)).all()
    )
    ts = datetime.utcnow()

    stats = {
        "real_services": len(services),
        "fetched": 0,
        "with_signal": 0,
        "inserted": 0,
        "skipped_empty": 0,
        "skipped_dup": 0,
        "errors": 0,
    }

    for svc in services:
        try:
            metrics = await _fetch_service_metrics(vm, svc.namespace, svc.name)
            stats["fetched"] += 1
        except Exception as e:
            stats["errors"] += 1
            log.warning(
                "metrics_sync.fetch_failed ns=%s name=%s err=%s",
                svc.namespace, svc.name, e,
            )
            continue

        if not _has_any_signal(metrics):
            stats["skipped_empty"] += 1
            continue

        stats["with_signal"] += 1
        inserted = _insert_idempotent(
            db, service_id=svc.id, ts=ts, metrics=metrics, source="vm",
        )
        if inserted:
            stats["inserted"] += 1
        else:
            stats["skipped_dup"] += 1

    db.commit()
    log.info(
        "metrics_sync.done real=%d fetched=%d with_signal=%d inserted=%d "
        "skipped_empty=%d skipped_dup=%d errors=%d",
        stats["real_services"], stats["fetched"], stats["with_signal"],
        stats["inserted"], stats["skipped_empty"], stats["skipped_dup"],
        stats["errors"],
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
