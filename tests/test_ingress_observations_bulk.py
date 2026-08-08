"""Синк ingress-наблюдений не должен масштабировать запросы по маршрутам.

Замер на проде 08.08.2026: 992 маршрута × 5 метрик = ~5000 последовательных
HTTP-вызовов в VictoriaMetrics за один тик. Воркеры уходили в это надолго, и
очередь Celery доросла до 230 задач — `kg_topology_resources_sync` в ней
стоял и не выполнялся вовсе, из-за чего `serves_traffic` оставался на трёх
рёбрах при уже исправленном коде.

PromQL умеет отдать все маршруты разом (`sum by(host,path)`), поэтому запросов
должно быть ровно пять — сколько метрик, а не сколько маршрутов.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import ingress_observations_sync as ios
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import IngressObservation


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()


def _mk_ingress(name: str, ns: str, routes):
    """routes: [(host, path, backend)]"""
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "rules": [
                {
                    "host": host,
                    "http": {"paths": [
                        {"path": path, "backend": {"service": {"name": backend}}},
                    ]},
                }
                for host, path, backend in routes
            ],
        },
    }


def _run(db, ingresses, vm_payload):
    """Прогнать синк, вернуть (stats, число обращений к VM)."""
    calls: list[str] = []

    async def fake_by_labels(query, by_labels):
        calls.append(query)
        metric = (
            "p95_latency_ms" if "0.95" in query else
            "p99_latency_ms" if "0.99" in query else
            "error_5xx_rate" if 'status=~"5' in query else
            "error_4xx_rate" if 'status=~"4' in query else
            "rps"
        )
        return {k: v[metric] for k, v in vm_payload.items() if metric in v}

    with patch.object(ios, "_kubectl_get_ingresses_all", return_value=ingresses), \
         patch.object(ios.settings, "VICTORIA_METRICS_URL", "http://vm.test"), \
         patch("app.knowledge_graph.ingress_observations_sync.VMClient") as VM:
        VM.return_value.query_instant_by_labels = AsyncMock(side_effect=fake_by_labels)
        stats = ios.sync_ingress_observations(db)
    return stats, calls


def test_query_count_does_not_grow_with_routes(db):
    """50 маршрутов — по-прежнему пять запросов.

    Это главное свойство правки. Если кто-то вернёт точечный запрос внутрь
    цикла, тест упадёт: станет 250 вызовов.
    """
    routes = [(f"h{i}.example.com", "/api", f"svc-{i}") for i in range(50)]
    ing = [_mk_ingress("big", "prod", routes)]
    payload = {
        (h, p): {"rps": 1.0, "p95_latency_ms": 100.0}
        for h, p, _ in routes
    }

    stats, calls = _run(db, ing, payload)

    assert len(calls) == 5, (
        f"ожидалось 5 запросов на все маршруты, получено {len(calls)} — "
        "похоже, запрос снова шлётся per-route"
    )
    assert stats["routes_seen"] == 50
    assert stats["inserted"] == 50


def test_metrics_land_on_the_right_route(db):
    """Значения раскладываются по своим (host, path), а не перемешиваются."""
    routes = [("a.example.com", "/one", "svc-a"), ("b.example.com", "/two", "svc-b")]
    ing = [_mk_ingress("two", "prod", routes)]
    payload = {
        ("a.example.com", "/one"): {"rps": 10.0, "error_5xx_rate": 0.5},
        ("b.example.com", "/two"): {"rps": 20.0, "p95_latency_ms": 700.0},
    }

    _run(db, ing, payload)

    rows = {r.host: r for r in db.query(IngressObservation).all()}
    assert rows["a.example.com"].rps == 10.0
    assert rows["a.example.com"].error_5xx_rate == 0.5
    assert rows["b.example.com"].rps == 20.0
    assert rows["b.example.com"].p95_latency_ms == 700.0


def test_route_without_metrics_is_skipped_not_zeroed(db):
    """Маршрут, по которому VM молчит, не пишется нулями.

    «Нет данных» ≠ «ноль запросов»: строка с нулями выглядела бы как
    исправный маршрут без трафика.
    """
    routes = [("live.example.com", "/", "svc-live"), ("quiet.example.com", "/", "svc-quiet")]
    ing = [_mk_ingress("mixed", "prod", routes)]
    payload = {("live.example.com", "/"): {"rps": 3.0}}

    stats, _ = _run(db, ing, payload)

    hosts = {r.host for r in db.query(IngressObservation).all()}
    assert hosts == {"live.example.com"}
    assert stats["skipped_empty"] == 1


def test_backend_service_is_linked(db):
    """service_id проставляется из предзагруженной карты (namespace, name)."""
    svc = upsert_service(db, "prod", "svc-a")
    db.commit()

    ing = [_mk_ingress("one", "prod", [("a.example.com", "/", "svc-a")])]
    payload = {("a.example.com", "/"): {"rps": 1.0}}

    _run(db, ing, payload)

    row = db.query(IngressObservation).one()
    assert row.service_id == svc.id
