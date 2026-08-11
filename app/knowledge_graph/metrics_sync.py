"""Sync per-service метрик из VictoriaMetrics → kg_service_health.

Beat-task `kg_metrics_sync` каждые ~10 мин:
1. Берём все real (synthetic=False) services из KG, группируем по namespace.
2. Для каждого namespace делаем 5 PromQL-запросов АГРЕГИРОВАННО:
     - cpu/mem/restarts: `... by (pod)` — одна серия на pod;
     - 5xx/p95:          `... by (service)` — одна серия на k8s-service.
   Затем pod → service маппится по longest-prefix против известных имён
   сервисов namespace (pod `bot-service-7d9f-x2k` → `bot-service`).
3. Если хоть одна метрика сервиса > 0 — пишем строку. Полностью нулевые
   ряды не вставляем (экспортёр не покрывает сервис).

Почему namespace-агрегация (recon 2026-06-05): прежняя схема делала
2463 svc × 5 PromQL ≈ 12300 запросов per-service к одиночному vmsingle.
Проход не укладывался в окно, таск умирал после первого commit-батча
(ровно 250 строк), покрытие health ~25%. Namespace-агрегация: ~77 ns × 5
≈ 385 запросов → полный проход за секунды, покрытие ~100%.

Идемпотентность: UNIQUE(service_id, ts); commit батчами. Per-namespace
exceptions ловятся — один проблемный namespace не валит весь sync.

CLI: `python -m app.knowledge_graph.metrics_sync`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, Service,
                                        ServiceHealth)

log = logging.getLogger(__name__)


# ── PromQL шаблоны per namespace ───────────────────────────────────────────
# cpu/mem/restarts группируются `by (pod)` — pod label потом маппится в
# имя сервиса (deployment/statefulset) по longest-prefix. 5xx/p95 группируются
# `by (service)` (label http_requests_total) и матчатся по имени сервиса.

def _q_ns_cpu_by_pod(namespace: str) -> str:
    return (
        f'avg by (pod) (rate(container_cpu_usage_seconds_total'
        f'{{namespace="{namespace}",container!=""}}[5m])) * 100'
    )


def _q_ns_mem_by_pod(namespace: str) -> str:
    # working_set / (limit OR request), per pod.
    # cAdvisor даёт несколько ts на (pod,container) → агрегируем sum by(pod)
    # слева; у большинства WO pods нет memory limit → fallback на request.
    ws = (
        f'sum by(pod) (container_memory_working_set_bytes'
        f'{{namespace="{namespace}",container!=""}})'
    )
    limit = (
        f'sum by(pod) (kube_pod_container_resource_limits'
        f'{{namespace="{namespace}",resource="memory"}})'
    )
    request = (
        f'sum by(pod) (kube_pod_container_resource_requests'
        f'{{namespace="{namespace}",resource="memory"}})'
    )
    return (
        f'100 * ({ws} / on(pod) '
        f'(({limit}) or on(pod) ({request})))'
    )


def _q_ns_restarts_by_pod(namespace: str) -> str:
    return (
        f'sum by (pod) (rate(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}"}}[15m])) * 60'
    )


def _q_ns_5xx_by_service(namespace: str) -> str:
    # ВАЖНО (upd 2026-06-10): nginx-ingress метрики теперь собираются (см.
    # kg_ingress_observations), но per-service http_requests_total в app-ns
    # по-прежнему НЕТ — /metrics сервисов закрыт JWT (401), скрейп ждёт
    # бэкенд-тикета WO-12483. До его раскатки запрос вернёт пусто (→ 0).
    return (
        f'sum by (service) (rate(http_requests_total'
        f'{{namespace="{namespace}",status=~"5.."}}[5m]))'
    )


def _q_ns_p95_by_service(namespace: str) -> str:
    # См. выше — app /metrics за JWT (WO-12483); запрос валидный, сейчас пусто.
    return (
        f'1000 * histogram_quantile(0.95, sum by(le, service)('
        f'rate(http_request_duration_seconds_bucket'
        f'{{namespace="{namespace}"}}[5m])'
        f'))'
    )


def _map_pod_to_service(pod: str, service_names_by_len: List[str]) -> Optional[str]:
    """pod → имя сервиса по longest-prefix.

    `service_names_by_len` отсортирован по убыванию длины — первый матч
    самый специфичный (town-db-postgresql-metrics раньше town-db-postgresql).
    Матч: pod == name ИЛИ pod начинается с `name-`.
    """
    for name in service_names_by_len:
        if pod == name or pod.startswith(name + "-"):
            return name
    return None


def _has_any_signal(metrics: Dict[str, Optional[float]]) -> bool:
    """True если хоть одна метрика > 0 (значит экспортёр накрывает сервис)."""
    return any((v is not None and v > 0.0) for v in metrics.values())


def _insert_idempotent(
    db: Session,
    service_id: int,
    ts: datetime,
    metrics: Dict[str, Optional[float]],
    source: str,
) -> bool:
    """INSERT с защитой по UNIQUE(service_id, ts). True если строка вставилась."""
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


async def _fetch_namespace(
    sem: asyncio.Semaphore,
    vm: VMClient,
    namespace: str,
) -> Tuple[str, Optional[Dict[str, Dict[str, float]]], Optional[BaseException]]:
    """Собрать 5 агрегированных метрик для одного namespace.

    Возвращает `(namespace, {metric: {key: value}} | None, exc | None)`.
    cpu/mem/restarts ключуются по pod, 5xx/p95 — по service-label.
    Исключение сериализуется в результат — gather не падает.
    """
    async with sem:
        try:
            by_pod = await asyncio.gather(
                vm.query_instant_by(_q_ns_cpu_by_pod(namespace), "pod"),
                vm.query_instant_by(_q_ns_mem_by_pod(namespace), "pod"),
                vm.query_instant_by(_q_ns_restarts_by_pod(namespace), "pod"),
                vm.query_instant_by(_q_ns_5xx_by_service(namespace), "service"),
                vm.query_instant_by(_q_ns_p95_by_service(namespace), "service"),
            )
            return (
                namespace,
                {
                    "cpu_pct": by_pod[0],
                    "mem_pct": by_pod[1],
                    "restarts_rate": by_pod[2],
                    "http_5xx_rate": by_pod[3],
                    "p95_latency_ms": by_pod[4],
                },
                None,
            )
        except BaseException as e:  # noqa: BLE001 — фиксируем всё, классифицируем выше
            return (namespace, None, e)


def _aggregate_service_metrics(
    raw: Dict[str, Dict[str, float]],
    services_in_ns: List[Tuple[int, str]],
) -> List[Tuple[int, str, Dict[str, Optional[float]]]]:
    """Свести namespace-метрики к per-service.

    raw: {metric: {pod_or_service_key: value}}.
    services_in_ns: [(service_id, service_name)].
    Возвращает [(service_id, name, metrics)] для всех сервисов namespace.

    Агрегация по нескольким pod одного сервиса: cpu/mem — mean, restarts —
    sum. 5xx/p95 — прямой матч по service-label (имя сервиса).
    """
    names_by_len = sorted((n for _, n in services_in_ns), key=len, reverse=True)
    # pod-уровневые метрики → собираем списки значений на имя сервиса
    cpu_acc: Dict[str, List[float]] = defaultdict(list)
    mem_acc: Dict[str, List[float]] = defaultdict(list)
    rst_acc: Dict[str, List[float]] = defaultdict(list)
    for metric_key, acc in (
        ("cpu_pct", cpu_acc), ("mem_pct", mem_acc), ("restarts_rate", rst_acc),
    ):
        for pod, val in raw.get(metric_key, {}).items():
            svc = _map_pod_to_service(pod, names_by_len)
            if svc is not None:
                acc[svc].append(val)

    svc_5xx = raw.get("http_5xx_rate", {})
    svc_p95 = raw.get("p95_latency_ms", {})

    out: List[Tuple[int, str, Dict[str, Optional[float]]]] = []
    for sid, name in services_in_ns:
        cpu = cpu_acc.get(name)
        mem = mem_acc.get(name)
        rst = rst_acc.get(name)
        metrics: Dict[str, Optional[float]] = {
            "cpu_pct": (sum(cpu) / len(cpu)) if cpu else None,
            "mem_pct": (sum(mem) / len(mem)) if mem else None,
            "restarts_rate": sum(rst) if rst else None,
            "http_5xx_rate": svc_5xx.get(name),
            "p95_latency_ms": svc_p95.get(name),
        }
        out.append((sid, name, metrics))
    return out


async def _sync_service_health_async(db: Session) -> Dict[str, Any]:
    if not settings.VICTORIA_METRICS_URL:
        log.info("metrics_sync.skipped reason=no_vm_url")
        return {"skipped": "no_vm_url"}

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=15.0)
    # node_kind='service': с contract 2.4 у пары «k8s Service foo + Deployment
    # foo» ДВА non-synthetic узла. Метрики агрегируются по имени, поэтому без
    # фильтра каждая пара писала бы две идентичные строки kg_service_health
    # каждые 10 минут (и удваивала queries/inserted в stats).
    services: List[Service] = (
        db.query(Service)
        .filter(
            Service.synthetic.is_(False),
            Service.node_kind == NODE_KIND_SERVICE,
        )
        .all()
    )
    ts = datetime.utcnow()

    concurrency = max(1, int(settings.KG_METRICS_SYNC_CONCURRENCY))
    stats: Dict[str, Any] = {
        "real_services": len(services),
        "concurrency": concurrency,
        "namespaces": 0,
        "queries": 0,
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

    # ── Группировка по namespace ───────────────────────────────────────────
    by_ns: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for s in services:
        by_ns[cast(str, s.namespace)].append((cast(int, s.id), cast(str, s.name)))
    namespaces = sorted(by_ns.keys())
    stats["namespaces"] = len(namespaces)
    stats["queries"] = len(namespaces) * 5

    # Read-транзакция от db.query(Service) выше обязана закончиться ДО
    # fetch-фазы: gather по VM занимает минуты, а PG убивает соединение,
    # висящее idle-in-transaction дольше 120с (database.py). Без commit
    # весь тик умирал в write-фазе с «server closed the connection
    # unexpectedly» — kg_metrics_sync стоял с 09.08.
    db.commit()

    # ── Fetch phase: по namespace, параллельно с semaphore-капом ───────────
    sem = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()
    results = await asyncio.gather(
        *[_fetch_namespace(sem, vm, ns) for ns in namespaces],
        return_exceptions=False,
    )
    fetch_elapsed = time.monotonic() - t0

    # ── Write phase: серийно (одна Session), commit батчами ────────────────
    COMMIT_BATCH = 500
    since_commit = 0
    for namespace, raw, exc in results:
        if exc is not None or raw is None:
            stats["errors"] += 1
            log.warning("metrics_sync.ns_failed ns=%s err=%s", namespace, exc)
            continue

        for sid, _name, metrics in _aggregate_service_metrics(raw, by_ns[namespace]):
            stats["fetched"] += 1
            if not _has_any_signal(metrics):
                stats["skipped_empty"] += 1
                continue
            stats["with_signal"] += 1
            if _insert_idempotent(db, service_id=sid, ts=ts, metrics=metrics, source="vm"):
                stats["inserted"] += 1
                since_commit += 1
            else:
                stats["skipped_dup"] += 1
            if since_commit >= COMMIT_BATCH:
                try:
                    db.commit()
                except Exception:
                    # Падение batch-commit (например, deadlock/serialization)
                    # оставляет Session в aborted-состоянии — без rollback
                    # все последующие inserts + финальный commit упадут с
                    # PendingRollbackError, теряя весь оставшийся проход.
                    db.rollback()
                    raise
                since_commit = 0

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    stats["duration_ms"] = int((time.monotonic() - t0) * 1000)

    log.info(
        "metrics_sync.done real=%d ns=%d queries=%d concurrency=%d fetched=%d "
        "with_signal=%d inserted=%d skipped_empty=%d skipped_dup=%d errors=%d "
        "fetch_ms=%d total_ms=%d",
        stats["real_services"], stats["namespaces"], stats["queries"], concurrency,
        stats["fetched"], stats["with_signal"], stats["inserted"],
        stats["skipped_empty"], stats["skipped_dup"], stats["errors"],
        int(fetch_elapsed * 1000), stats["duration_ms"],
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
