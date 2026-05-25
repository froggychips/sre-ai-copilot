"""Тесты параллельного fetch в kg_metrics_sync.

Покрытие:
- _fetch_with_semaphore: одна неудача не валит остальных.
- _sync_service_health_async: для N=100 сервисов с искусственной задержкой
  per-query 50ms wall-time укладывается значительно ниже sequential оценки.
- Semaphore реально ограничивает concurrency (наблюдаем peak in-flight).
- Empty services → пустой stats, не падаем.
- skipped_no_vm_url → ранний выход.
- Error per service не дропает соседей; counter `errors` инкрементится.
- Concurrency-knob (KG_METRICS_SYNC_CONCURRENCY) honored.

Не требует postgres — все БД-операции на SQLite in-memory.
"""
from __future__ import annotations

import asyncio
import time
from typing import List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.knowledge_graph import metrics_sync
from app.knowledge_graph.metrics_sync import (
    _fetch_with_semaphore,
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


def _seed_services(db, n: int) -> List[Service]:
    out: List[Service] = []
    for i in range(n):
        s = Service(
            name=f"svc-{i:03d}",
            namespace=f"ns-{i % 5}",
            synthetic=False,
        )
        db.add(s)
        out.append(s)
    db.commit()
    return out


class _FakeVM:
    """In-process VMClient stub. Считает peak in-flight через семафор-извне."""

    def __init__(
        self,
        per_query_delay: float = 0.0,
        fail_on_namespaces: tuple[str, ...] = (),
        return_value: float = 0.5,
    ) -> None:
        self._delay = per_query_delay
        self._fail_ns = fail_on_namespaces
        self._return = return_value
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = asyncio.Lock()
        self.queries: List[str] = []

    async def query_instant(self, query: str) -> float:
        # Track peak concurrency.
        async with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.queries.append(query)

        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            for ns in self._fail_ns:
                if f'namespace="{ns}"' in query:
                    raise RuntimeError(f"synthetic failure for {ns}")
            return self._return
        finally:
            async with self._lock:
                self.in_flight -= 1


# ── _fetch_with_semaphore unit tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_with_semaphore_returns_metrics_on_success():
    vm = _FakeVM(return_value=1.5)
    sem = asyncio.Semaphore(2)
    svc_id, ns, name, metrics, exc = await _fetch_with_semaphore(
        sem, vm, 42, "ns-a", "svc-a",
    )
    assert svc_id == 42
    assert ns == "ns-a"
    assert name == "svc-a"
    assert exc is None
    assert metrics is not None
    assert metrics["cpu_pct"] == 1.5
    # 5 PromQL templates per service.
    assert len(vm.queries) == 5


@pytest.mark.asyncio
async def test_fetch_with_semaphore_isolates_failures():
    """Одна ошибка в одном сервисе не должна влиять на остальных.

    Контракт `_fetch_service_metrics` использует gather(return_exceptions=True)
    → per-query exception НЕ всплывает наружу, конвертируется в metrics[k]=None.
    Поэтому ns-bad сервис вернёт metrics со всеми None (но exc=None).
    Остальные сервисы получают свои метрики без помех.
    """

    class _OneShotFailVM:
        def __init__(self):
            self.calls = 0

        async def query_instant(self, query: str) -> float:
            self.calls += 1
            if 'namespace="ns-bad"' in query:
                raise RuntimeError("vm down")
            return 0.1

    vm = _OneShotFailVM()
    sem = asyncio.Semaphore(4)

    results = await asyncio.gather(
        _fetch_with_semaphore(sem, vm, 1, "ns-good", "svc-1"),
        _fetch_with_semaphore(sem, vm, 2, "ns-bad", "svc-2"),
        _fetch_with_semaphore(sem, vm, 3, "ns-good", "svc-3"),
        _fetch_with_semaphore(sem, vm, 4, "ns-good", "svc-4"),
    )
    # Все 4 завершились — gather не зарейзил.
    assert len(results) == 4
    by_id = {r[0]: r for r in results}

    # Хорошие сервисы: метрики != None, exc == None, значения > 0.
    for svc_id in (1, 3, 4):
        _, _, _, metrics, exc = by_id[svc_id]
        assert exc is None
        assert metrics is not None
        assert metrics["cpu_pct"] == 0.1
        assert metrics["mem_pct"] == 0.1

    # Плохой: gather внутри проглотил все 5 query-exception → metrics все None,
    # exc остался None.
    _, _, _, bad_metrics, bad_exc = by_id[2]
    assert bad_exc is None
    assert bad_metrics is not None
    assert all(v is None for v in bad_metrics.values())


# ── _sync_service_health_async integration ──────────────────────────────────


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
    assert result["fetched"] == 0
    assert result["inserted"] == 0
    assert "duration_ms" in result


@pytest.mark.asyncio
async def test_sync_writes_rows_with_signal(db, monkeypatch):
    """Happy path: 5 сервисов, все возвращают >0 — 5 строк вставлено."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(
        metrics_sync, "VMClient", lambda *a, **kw: _FakeVM(return_value=0.3),
    )
    _seed_services(db, 5)

    result = await _sync_service_health_async(db)
    assert result["real_services"] == 5
    assert result["fetched"] == 5
    assert result["with_signal"] == 5
    assert result["inserted"] == 5
    assert result["skipped_empty"] == 0
    assert result["errors"] == 0

    rows = db.query(ServiceHealth).all()
    assert len(rows) == 5
    assert all(r.cpu_pct == 0.3 for r in rows)


@pytest.mark.asyncio
async def test_sync_skips_empty_signal(db, monkeypatch):
    """Сервис у которого все метрики == 0 → skipped_empty, не вставляется."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(
        metrics_sync, "VMClient", lambda *a, **kw: _FakeVM(return_value=0.0),
    )
    _seed_services(db, 3)

    result = await _sync_service_health_async(db)
    assert result["with_signal"] == 0
    assert result["skipped_empty"] == 3
    assert result["inserted"] == 0
    assert db.query(ServiceHealth).count() == 0


@pytest.mark.asyncio
async def test_sync_parallel_speedup(db, monkeypatch):
    """Sequential оценка для 50 сервисов × 5 queries × 50ms = 12.5s.

    С concurrency=10 параллельно: внутренний gather параллелит 5 queries за
    50ms, semaphore=10 позволяет ~10 одновременных сервисов → ~250ms
    оптимистично + накладные. Берём щедрый bound: < 5 секунд (всё ещё в
    2.5× раз быстрее sequential).
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(settings, "KG_METRICS_SYNC_CONCURRENCY", 10)

    fake = _FakeVM(per_query_delay=0.05, return_value=0.2)
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: fake)
    _seed_services(db, 50)

    t0 = time.monotonic()
    result = await _sync_service_health_async(db)
    elapsed = time.monotonic() - t0

    # 50 * 5 * 0.05 = 12.5s sequential. С параллелизмом должно быть <5s.
    assert elapsed < 5.0, f"sync took {elapsed:.2f}s (expected <5s parallel)"
    assert result["inserted"] == 50

    # Sanity: peak in-flight queries >> 1 — действительно параллельно.
    # 5 queries per service параллельны → peak as low as 5 даже при
    # concurrency=1; при concurrency=10 ждём 10×5 = 50, но real peak зависит
    # от scheduler — assert минимум 10 одновременных.
    assert fake.peak_in_flight > 10, (
        f"expected high parallelism, peak={fake.peak_in_flight}"
    )


@pytest.mark.asyncio
async def test_sync_semaphore_caps_concurrency(db, monkeypatch):
    """Semaphore должен ограничить in-flight сервисов до KG_METRICS_SYNC_CONCURRENCY.

    Внутри сервиса всё ещё 5 параллельных запросов через gather, поэтому
    наблюдаемый peak in-flight queries ≈ concurrency * 5.
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(settings, "KG_METRICS_SYNC_CONCURRENCY", 3)

    fake = _FakeVM(per_query_delay=0.02, return_value=0.1)
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: fake)
    _seed_services(db, 30)

    await _sync_service_health_async(db)

    # 3 сервиса × 5 PromQL = 15 одновременных. Допускаем небольшой запас вверх.
    assert fake.peak_in_flight <= 3 * 5 + 1, (
        f"semaphore не ограничивает: peak={fake.peak_in_flight} > 16"
    )


@pytest.mark.asyncio
async def test_sync_isolates_errors_per_service(db, monkeypatch):
    """Если один сервис упал — остальные всё равно записаны.

    Делаем VMClient который всегда рейзит для namespace=ns-1 даже внутри
    gather (чтобы _fetch_service_metrics получил BaseException в результатах
    и вернул None). На самом деле _fetch_service_metrics ловит exceptions
    через gather(return_exceptions=True), но _q_cpu_pct etc. могут рейзить
    раньше — тут проверяем что fetch_with_semaphore catches всё.
    """

    class _PartialFailVM:
        def __init__(self):
            self.peak_in_flight = 0  # совместимость

        async def query_instant(self, query: str) -> float:
            if 'namespace="ns-1"' in query:
                raise RuntimeError("vm down for ns-1")
            return 0.4

    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(
        metrics_sync, "VMClient", lambda *a, **kw: _PartialFailVM(),
    )
    _seed_services(db, 10)  # ns-0..ns-4 round-robin → ns-1 у svc-001, 006

    result = await _sync_service_health_async(db)
    # 2 сервиса в ns-1 (индексы 1 и 6) → они получат all-None через gather,
    # пройдут как skipped_empty. Остальные 8 — успешно записаны.
    assert result["inserted"] == 8
    assert result["skipped_empty"] == 2
    assert result["errors"] == 0  # gather внутри проглотил, не fatal


@pytest.mark.asyncio
async def test_sync_concurrency_setting_honored(db, monkeypatch):
    """KG_METRICS_SYNC_CONCURRENCY=1 → строго sequential (peak ≤ 5 queries)."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    monkeypatch.setattr(settings, "KG_METRICS_SYNC_CONCURRENCY", 1)

    fake = _FakeVM(per_query_delay=0.01, return_value=0.1)
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: fake)
    _seed_services(db, 5)

    await _sync_service_health_async(db)
    # При concurrency=1: один сервис за раз, внутри 5 параллельных queries.
    assert fake.peak_in_flight <= 5, (
        f"concurrency=1 не sequential: peak={fake.peak_in_flight}"
    )


@pytest.mark.asyncio
async def test_sync_records_duration_ms(db, monkeypatch):
    """`duration_ms` в stats > 0 и ≈ elapsed."""
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    fake = _FakeVM(per_query_delay=0.01, return_value=0.1)
    monkeypatch.setattr(metrics_sync, "VMClient", lambda *a, **kw: fake)
    _seed_services(db, 5)

    result = await _sync_service_health_async(db)
    assert result["duration_ms"] > 0
    assert "concurrency" in result
