"""Sync ingress endpoint метрик из VM → kg_ingress_observations.

Beat-task `kg_ingress_observations_sync` каждые ~10 мин:
1. Источник host/path — kubectl get ingresses -A (через helper из
   k8s_ingress_sync). Реюзаем, чтобы не дублировать парсинг манифестов.
2. Backend service резолвим через kg_services (namespace + name из ingress
   backend). Если не найден — service_id остаётся NULL, ряд всё равно
   пишем (полезно для observability ingress'а даже без KG-резолва).
3. Для каждого (host, path) запрашиваем p95/p99/rps/4xx/5xx из VM.
4. Если ВСЕ метрики == 0 — пропускаем (экспортёр nginx-ingress, скорее
   всего, не накрывает endpoint).

Идемпотентность: UNIQUE(ingress_name, host, path, ts).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.k8s_ingress_sync import (_extract_routes,
                                                  _kubectl_get_ingresses_all)
from app.knowledge_graph.schema import NODE_KIND_SERVICE, IngressObservation, Service

log = logging.getLogger(__name__)


# ── PromQL шаблоны nginx-ingress controller ──────────────────────────────
# Метрики экспортируются nginx-ingress-controller с лейблами host/ingress/path.
# Если у вас другой ingress controller (traefik / istio) — придётся править
# имена метрик. Сейчас под WO — nginx.

# Запросы агрегируют СРАЗУ ПО ВСЕМ маршрутам: `by (host, path)` возвращает
# все пары одним ответом. Раньше на каждый маршрут слался свой точечный
# запрос — при 992 маршрутах это 992 × 5 = ~5000 HTTP-вызовов за тик. Воркер
# уходил в них надолго и вытеснял остальные синки: замер 08.08.2026 показал
# очередь из 230 задач, в которой kg_topology_resources_sync не выполнялся
# вовсе. Теперь пять запросов на весь тик, независимо от числа маршрутов.

def _q_p95_latency_ms() -> str:
    return (
        '1000 * histogram_quantile(0.95, sum by(host,path,le)('
        'rate(nginx_ingress_controller_request_duration_seconds_bucket[5m])'
        '))'
    )


def _q_p99_latency_ms() -> str:
    return (
        '1000 * histogram_quantile(0.99, sum by(host,path,le)('
        'rate(nginx_ingress_controller_request_duration_seconds_bucket[5m])'
        '))'
    )


def _q_rps() -> str:
    return 'sum by(host,path)(rate(nginx_ingress_controller_requests[5m]))'


def _q_err_rate(status_class: str) -> str:
    # status_class: "5" или "4". status — full code, regex по prefix.
    return (
        f'sum by(host,path)(rate(nginx_ingress_controller_requests'
        f'{{status=~"{status_class}.."}}[5m]))'
    )


async def _fetch_all_ingress_metrics(
    vm: VMClient,
) -> Dict[Tuple[str, str], Dict[str, Optional[float]]]:
    """Снять все пять метрик по всем маршрутам за пять запросов.

    Возвращает `{(host, path): {metric: value}}`. Маршруты, по которым VM
    ничего не отдала, в результате отсутствуют — вызывающий код трактует это
    как «нет сигнала», ровно как раньше трактовал пустой точечный ответ.
    """
    queries = {
        "p95_latency_ms": _q_p95_latency_ms(),
        "p99_latency_ms": _q_p99_latency_ms(),
        "rps": _q_rps(),
        "error_5xx_rate": _q_err_rate("5"),
        "error_4xx_rate": _q_err_rate("4"),
    }
    keys = list(queries.keys())
    results = await asyncio.gather(
        *[vm.query_instant_by_labels(q, ("host", "path")) for q in queries.values()],
        return_exceptions=True,
    )

    out: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
    for metric_name, res in zip(keys, results):
        if isinstance(res, BaseException):
            log.warning(
                "ingress_observations_sync.bulk_query_failed metric=%s err=%s",
                metric_name, res,
            )
            continue
        for (host, path), value in res.items():
            out.setdefault((host, path), {})[metric_name] = value
    return out


def _has_any_signal(metrics: Dict[str, Optional[float]]) -> bool:
    return any((v is not None and v > 0.0) for v in metrics.values())


def _insert_idempotent(
    db: Session,
    *,
    ts: datetime,
    ingress_name: str,
    host: str,
    path: str,
    service_id: Optional[int],
    metrics: Dict[str, Optional[float]],
) -> bool:
    row = IngressObservation(
        ts=ts,
        ingress_name=ingress_name,
        host=host,
        path=path,
        service_id=service_id,
        p95_latency_ms=metrics.get("p95_latency_ms"),
        p99_latency_ms=metrics.get("p99_latency_ms"),
        rps=metrics.get("rps"),
        error_5xx_rate=metrics.get("error_5xx_rate"),
        error_4xx_rate=metrics.get("error_4xx_rate"),
    )
    try:
        with db.begin_nested():
            db.add(row)
        return True
    except IntegrityError:
        return False


async def _sync_ingress_observations_async(db: Session) -> Dict[str, Any]:
    if not settings.VICTORIA_METRICS_URL:
        log.info("ingress_observations_sync.skipped reason=no_vm_url")
        return {"skipped": "no_vm_url"}

    ingresses: List[Dict[str, Any]] = _kubectl_get_ingresses_all()
    if not ingresses:
        log.info("ingress_observations_sync.no_ingresses")
        return {"ingresses": 0, "inserted": 0}

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=30.0)
    ts = datetime.utcnow()

    # Пять агрегирующих запросов на весь тик — ДО обхода маршрутов. Раньше
    # запрос слался внутри цикла на каждый маршрут, и обход 992 маршрутов
    # превращался в ~5000 последовательных HTTP-вызовов.
    metrics_by_route = await _fetch_all_ingress_metrics(vm)

    stats = {
        "ingresses": len(ingresses),
        "routes_seen": 0,
        "fetched": 0,
        "with_signal": 0,
        "inserted": 0,
        "skipped_empty": 0,
        "skipped_dup": 0,
        "errors": 0,
    }

    # Резолв backend-сервисов одним запросом. Точечный lookup внутри цикла
    # давал ещё 992 обращения к БД поверх HTTP-вызовов.
    backend_ids: Dict[Tuple[str, str], int] = {
        (str(ns), str(name)): int(sid)
        for ns, name, sid in db.query(
            Service.namespace, Service.name, Service.id,
        ).filter(Service.node_kind == NODE_KIND_SERVICE).all()
    }

    for ing in ingresses:
        meta = ing.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        ing_name = meta.get("name") or "?"

        routes = _extract_routes(ing)
        for r in routes:
            stats["routes_seen"] += 1
            host = r["host"]
            path = r.get("path") or "/"
            backend_name = r["backend"]

            service_id: Optional[int] = backend_ids.get((ns, backend_name))

            metrics = metrics_by_route.get((host, path))
            if metrics is None:
                # VM не вернула ни одной серии по этой паре — маршрут есть в
                # k8s, но трафика/метрик по нему нет.
                stats["skipped_empty"] += 1
                continue
            stats["fetched"] += 1

            if not _has_any_signal(metrics):
                stats["skipped_empty"] += 1
                continue

            stats["with_signal"] += 1
            ok = _insert_idempotent(
                db,
                ts=ts,
                ingress_name=ing_name,
                host=host,
                path=path,
                service_id=service_id,
                metrics=metrics,
            )
            if ok:
                stats["inserted"] += 1
            else:
                stats["skipped_dup"] += 1

    db.commit()
    log.info(
        "ingress_observations_sync.done ingresses=%d routes=%d inserted=%d "
        "skipped_empty=%d skipped_dup=%d errors=%d",
        stats["ingresses"], stats["routes_seen"], stats["inserted"],
        stats["skipped_empty"], stats["skipped_dup"], stats["errors"],
    )
    return stats


def sync_ingress_observations(db: Session) -> Dict[str, Any]:
    """Sync-обёртка для Celery."""
    return asyncio.run(_sync_ingress_observations_async(db))


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(sync_ingress_observations(db))
    finally:
        db.close()
