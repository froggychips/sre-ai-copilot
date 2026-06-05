"""Тесты namespace-агрегированного kg_metrics_sync.

Покрытие:
- _map_pod_to_service: longest-prefix маппинг pod → имя сервиса.
- _aggregate_service_metrics: pod-метрики → per-service (cpu/mem mean,
  restarts sum), 5xx/p95 по service-label.
- _sync_service_health_async: happy-path запись, skip полностью-нулевых,
  изоляция упавшего namespace, semaphore-кап concurrency, no_vm_url/no_svc.

Namespace-агрегация (recon 2026-06-05): вместо 2463 svc × 5 PromQL per-service
делаем ~ns × 5 запросов `by(pod)`/`by(service)`. Не требует postgres —
всё на SQLite in-memory.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.knowledge_graph import metrics_sync
from app.knowledge_graph.metrics_sync import (
    _aggregate_service_metrics,
    _map_pod_to_service,
    _sync_service_health_async,
)
from app.knowledge_graph.schema import Service, ServiceHealth


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_services(db, specs: List[tuple]) -> List[Service]:
    """specs: [(name, namespace), ...]. Возвращает созданные Service."""
    out: List[Service] = []
    for name, ns in specs:
        s = Service(name=name, namespace=ns, synthetic=False)
        db.add(s)
        out.append(s)
    db.commit()
    return out


_NS_RE = re.compile(r'namespace="([^"]+)"')


class _FakeVM:
    """In-process VMClient stub для query_instant_by.

    ns_pods:     {namespace: [pod_name, ...]} — что вернут by(pod)-запросы.
    pod_value:   значение для каждого pod.
    fail_on:     namespaces, на которых любой запрос рейзит (gather внутри
                 _fetch_namespace проглотит — но мы тестим и явный raise через
                 fail_hard).
    """

    def __init__(self, ns_pods=None, pod_value=0.5,
                 fail_on=(), per_query_delay=0.0):
        self._ns_pods = ns_pods or {}
        self._val = pod_value
        self._fail = tuple(fail_on)
        self._delay = per_query_delay
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = asyncio.Lock()
        self.queries: List[str] = []

    async def query_instant_by(self, query: str, by_label: str) -> Dict[str, float]:
        async with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.queries.append(query)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            m = _NS_RE.search(query)
            ns = m.group(1) if m else ""
            if ns in self._fail:
                raise RuntimeError(f"synthetic failure for {ns}")
            # 5xx/p95 (by service) — пусто, как в текущем кластере.
            if by_label == "service":
                return {}
            return {pod: self._val for pod in self._ns_pods.get(ns, [])}
        finally:
            async with self._lock:
                self.in_flight -= 1


# ── _map_pod_to_service ──────────────────────────────────────────────────────


def test_map_pod_to_service_basic():
    names = sorted(["bot-service", "town-service"], key=len, reverse=True)
    assert _map_pod_to_service("bot-service-7d9f8-x2k4l", names) == "bot-service"
    assert _map_pod_to_service("town-service-abc-def", names) == "town-service"
    assert _map_pod_to_service("unrelated-pod-xyz", names) is None


def test_map_pod_to_service_longest_prefix_wins():
    # town-db-postgresql vs town-db-postgresql-metrics — выбираем специфичный.
    names = sorted(
        ["town-db-postgresql", "town-db-postgresql-metrics"],
        key=len, reverse=True,
    )
    assert _map_pod_to_service(
        "town-db-postgresql-metrics-0", names,
    ) == "town-db-postgresql-metrics"
    assert _map_pod_to_service("town-db-postgresql-0", names) == "town-db-postgresql"


def test_map_pod_to_service_statefulset_exact():
    names = ["redis"]
    assert _map_pod_to_service("redis-0", names) == "redis"
    assert _map_pod_to_service("redis", names) == "redis"


# ── _aggregate_service_metrics ───────────────────────────────────────────────


def test_aggregate_mean_cpu_sum_restarts():
    raw = {
        "cpu_pct": {"bot-service-a": 2.0, "bot-service-b": 4.0},  # mean = 3.0
        "mem_pct": {"bot-service-a": 10.0, "bot-service-b": 30.0},  # mean = 20.0
        "restarts_rate": {"bot-service-a": 1.0, "bot-service-b": 2.0},  # sum = 3.0
        "http_5xx_rate": {},
        "p95_latency_ms": {},
    }
    out = _aggregate_service_metrics(raw, [(1, "bot-service")])
    assert len(out) == 1
    sid, name, m = out[0]
    assert sid == 1 and name == "bot-service"
    assert m["cpu_pct"] == 3.0
    assert m["mem_pct"] == 20.0
    assert m["restarts_rate"] == 3.0
    assert m["http_5xx_rate"] is None
    assert m["p95_latency_ms"] is None


def test_aggregate_service_without_pods_is_all_none():
    raw = {"cpu_pct": {}, "mem_pct": {}, "restarts_rate": {},
           "http_5xx_rate": {}, "p95_latency_ms": {}}
    out = _aggregate_service_metrics(raw, [(7, "lonely-service")])
    _, _, m = out[0]
    assert all(v is None for v in m.values())


def test_aggregate_5xx_by_service_label():
    raw = {"cpu_pct": {}, "mem_pct": {}, "restarts_rate": {},
           "http_5xx_rate": {"api-service": 0.42},
           "p95_latency_ms": {"api-service": 120.0}}
    out = _aggregate_service_metrics(raw, [(3, "api-service")])
    _, _, m = out[0]
    assert m["http_5xx_rate"] == 0.42
    assert m["p95_latency_ms"] == 120.0


# ── _sync_service_health_async integration ───────────────────────────────────


@pytest.mark.asyncio
async def test_sync_skipped_when_no_vm_url(db, monkeypatch):
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "")
    result = await _sync_service_health_async(db)
    assert result == {"skipped": "no_vm_url"}


@pytest.mark.asyncio
async def test_sync_returns_empty_stats_when_no_services(db, monkeypatch):
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: _FakeVM())
    result = await _sync_service_health_async(db)
    assert result["real_services"] == 0
    assert result["inserted"] == 0
    assert "duration_ms" in result


@pytest.mark.asyncio
async def test_sync_writes_rows_with_signal(db, monkeypatch):
    """3 сервиса в 2 ns; VM возвращает pod на каждый → 3 строки."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [
        ("bot-service", "prod-kingdom1"),
        ("town-service", "prod-kingdom1"),
        ("push-service", "prod-shared"),
    ])
    ns_pods = {
        "prod-kingdom1": ["bot-service-aaa-bbb", "town-service-ccc-ddd"],
        "prod-shared": ["push-service-eee-fff"],
    }
    monkeypatch.setattr(
        metrics_sync, "VMClient",
        lambda *a, **kw: _FakeVM(ns_pods=ns_pods, pod_value=0.3),
    )
    result = await _sync_service_health_async(db)
    assert result["real_services"] == 3
    assert result["namespaces"] == 2
    assert result["queries"] == 10  # 2 ns × 5
    assert result["with_signal"] == 3
    assert result["inserted"] == 3
    assert result["skipped_empty"] == 0
    assert result["errors"] == 0

    rows = db.query(ServiceHealth).all()
    assert len(rows) == 3
    assert all(r.cpu_pct == 0.3 for r in rows)


@pytest.mark.asyncio
async def test_sync_skips_empty_signal(db, monkeypatch):
    """Сервис без pod-метрик (VM пусто) → skipped_empty, не вставляется."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("a-service", "ns-1"), ("b-service", "ns-1")])
    # ns_pods пуст → by(pod) вернёт {} → все метрики None.
    monkeypatch.setattr(
        metrics_sync, "VMClient", lambda *a, **kw: _FakeVM(ns_pods={}),
    )
    result = await _sync_service_health_async(db)
    assert result["with_signal"] == 0
    assert result["skipped_empty"] == 2
    assert result["inserted"] == 0
    assert db.query(ServiceHealth).count() == 0


@pytest.mark.asyncio
async def test_sync_isolates_failed_namespace(db, monkeypatch):
    """Падение запросов одного namespace не валит остальные."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [
        ("ok-service", "ns-good"),
        ("bad-service", "ns-bad"),
    ])
    ns_pods = {
        "ns-good": ["ok-service-1"],
        "ns-bad": ["bad-service-1"],
    }
    # _fetch_namespace ловит BaseException → ns-bad даст errors+=1, ns-good пишется.
    monkeypatch.setattr(
        metrics_sync, "VMClient",
        lambda *a, **kw: _FakeVM(ns_pods=ns_pods, pod_value=0.2, fail_on=("ns-bad",)),
    )
    result = await _sync_service_health_async(db)
    assert result["errors"] == 1
    assert result["inserted"] == 1
    rows = db.query(ServiceHealth).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_sync_semaphore_caps_namespace_concurrency(db, monkeypatch):
    """Semaphore ограничивает одновременные namespace-фетчи.

    Внутри namespace 5 параллельных запросов через gather, поэтому peak
    in-flight ≈ concurrency × 5.
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(settings, "KG_METRICS_SYNC_CONCURRENCY", 2)
    specs = [(f"svc-{i}", f"ns-{i}") for i in range(20)]
    _seed_services(db, specs)
    ns_pods = {f"ns-{i}": [f"svc-{i}-pod"] for i in range(20)}
    fake = _FakeVM(ns_pods=ns_pods, pod_value=0.1, per_query_delay=0.02)
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: fake)

    await _sync_service_health_async(db)

    # 2 namespace × 5 запросов = 10 одновременных + небольшой запас.
    assert fake.peak_in_flight <= 2 * 5 + 1, (
        f"semaphore не ограничивает: peak={fake.peak_in_flight}"
    )


@pytest.mark.asyncio
async def test_sync_records_duration_ms(db, monkeypatch):
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc", "ns-1")])
    monkeypatch.setattr(
        metrics_sync, "VMClient",
        lambda *a, **kw: _FakeVM(ns_pods={"ns-1": ["svc-1"]}, pod_value=0.5),
    )
    result = await _sync_service_health_async(db)
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0
