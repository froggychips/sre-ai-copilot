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
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.vm_client import VMClient
from app.knowledge_graph.k8s_ingress_sync import (_extract_routes,
                                                  _kubectl_get_ingresses_all)
from app.knowledge_graph.schema import IngressObservation, Service

log = logging.getLogger(__name__)


# ── PromQL шаблоны nginx-ingress controller ──────────────────────────────
# Метрики экспортируются nginx-ingress-controller с лейблами host/ingress/path.
# Если у вас другой ingress controller (traefik / istio) — придётся править
# имена метрик. Сейчас под WO — nginx.

def _q_p95_latency_ms(host: str, path: str) -> str:
    return (
        f'1000 * histogram_quantile(0.95, sum by(le)('
        f'rate(nginx_ingress_controller_request_duration_seconds_bucket'
        f'{{host="{host}",path="{path}"}}[5m])'
        f'))'
    )


def _q_p99_latency_ms(host: str, path: str) -> str:
    return (
        f'1000 * histogram_quantile(0.99, sum by(le)('
        f'rate(nginx_ingress_controller_request_duration_seconds_bucket'
        f'{{host="{host}",path="{path}"}}[5m])'
        f'))'
    )


def _q_rps(host: str, path: str) -> str:
    return (
        f'sum(rate(nginx_ingress_controller_requests'
        f'{{host="{host}",path="{path}"}}[5m]))'
    )


def _q_err_rate(host: str, path: str, status_class: str) -> str:
    # status_class: "5" или "4". status — full code, regex по prefix.
    return (
        f'sum(rate(nginx_ingress_controller_requests'
        f'{{host="{host}",path="{path}",status=~"{status_class}.."}}[5m]))'
    )


async def _fetch_ingress_metrics(
    vm: VMClient, host: str, path: str,
) -> Dict[str, Optional[float]]:
    queries = {
        "p95_latency_ms": _q_p95_latency_ms(host, path),
        "p99_latency_ms": _q_p99_latency_ms(host, path),
        "rps": _q_rps(host, path),
        "error_5xx_rate": _q_err_rate(host, path, "5"),
        "error_4xx_rate": _q_err_rate(host, path, "4"),
    }
    keys = list(queries.keys())
    values = await asyncio.gather(
        *[vm.query_instant(q) for q in queries.values()],
        return_exceptions=True,
    )
    out: Dict[str, Optional[float]] = {}
    for k, v in zip(keys, values):
        # query_instant теперь отдаёт None при «нет данных» (а не 0.0).
        if isinstance(v, BaseException) or v is None:
            out[k] = None
            continue
        out[k] = float(v)
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

    vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=10.0)
    ts = datetime.utcnow()

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

            backend = (
                db.query(Service)
                .filter_by(namespace=ns, name=backend_name)
                .one_or_none()
            )
            service_id: Optional[int] = cast(int, backend.id) if backend else None

            try:
                metrics = await _fetch_ingress_metrics(vm, host, path)
                stats["fetched"] += 1
            except Exception as e:
                stats["errors"] += 1
                log.warning(
                    "ingress_observations_sync.fetch_failed host=%s path=%s err=%s",
                    host, path, e,
                )
                continue

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
