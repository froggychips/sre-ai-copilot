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
from typing import List

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
from app.knowledge_graph.schema import (NODE_KIND_WORKLOAD, Service,
                                        ServiceHealth)


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
    """In-process MetricsProvider stub.

    ns_pods:     {namespace: [pod_name, ...]} — что вернут by(pod)-запросы.
    pod_value:   значение для каждого pod.
    fail_on:     namespaces, по которым источник НЕ отвечает — провайдер
                 возвращает `Measurement.unknown`, и синк обязан считать
                 такой namespace неуспешным, а не пустым.
    """

    name = "fake-vm"

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

    async def by_label(self, query: str, label: str):
        from app.providers.measurement import Measurement

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
                return Measurement.unknown(f"vm_unavailable: synthetic {ns}")
            # 5xx/p95 (by service) — пусто, как в текущем кластере. Пустота
            # измерена: источник ответил, серий нет.
            if label == "service":
                return Measurement.of({})
            return Measurement.of({pod: self._val for pod in self._ns_pods.get(ns, [])})
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
    monkeypatch.setattr(metrics_sync, "make_metrics_provider", lambda *a, **kw: _FakeVM())
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
        metrics_sync, "make_metrics_provider",
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
        metrics_sync, "make_metrics_provider", lambda *a, **kw: _FakeVM(ns_pods={}),
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
        metrics_sync, "make_metrics_provider",
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
    monkeypatch.setattr(metrics_sync, "make_metrics_provider", lambda *a, **kw: fake)

    await _sync_service_health_async(db)

    # 2 namespace × 5 запросов = 10 одновременных + небольшой запас.
    assert fake.peak_in_flight <= 2 * 5 + 1, (
        f"semaphore не ограничивает: peak={fake.peak_in_flight}"
    )


@pytest.mark.asyncio
async def test_sync_ignores_workload_twin_node(db, monkeypatch):
    """Пара «k8s Service foo + workload foo» (contract 2.4) → ОДНА строка
    kg_service_health, привязанная к Service-узлу.

    Регрессия: без фильтра node_kind оба non-synthetic узла попадали в
    выборку, метрики агрегируются по имени → две идентичные строки на пару
    каждые 10 минут.
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    svc = Service(name="bot-service", namespace="prod-kingdom1", synthetic=False)
    twin = Service(name="bot-service", namespace="prod-kingdom1",
                   synthetic=False, node_kind=NODE_KIND_WORKLOAD)
    db.add_all([svc, twin])
    db.commit()
    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _FakeVM(
            ns_pods={"prod-kingdom1": ["bot-service-aaa-bbb"]}, pod_value=0.3,
        ),
    )
    result = await _sync_service_health_async(db)
    assert result["real_services"] == 1
    assert result["inserted"] == 1
    rows = db.query(ServiceHealth).all()
    assert len(rows) == 1
    assert rows[0].service_id == svc.id


@pytest.mark.asyncio
async def test_sync_records_duration_ms(db, monkeypatch):
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc", "ns-1")])
    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _FakeVM(ns_pods={"ns-1": ["svc-1"]}, pod_value=0.5),
    )
    result = await _sync_service_health_async(db)
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_blind_source_is_not_a_quiet_success(db, monkeypatch):
    """Недоступная VictoriaMetrics обязана считаться ошибкой, а не тишиной.

    До перевода на провайдер отказ глотался внутри клиента: `errors`
    оставался нулевым, сервисы получали None по всем метрикам и уходили в
    `skipped_empty`. Прогон выглядел образцовым — ошибок нет, просто ни у
    кого нет сигнала, — и отличить это от namespace без экспортёров было
    нельзя. Ровно та слепота, неотличимая от тишины, против которой стоит
    нулевой слой.
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc-a", "ns-one"), ("svc-b", "ns-two")])
    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _FakeVM(
            ns_pods={"ns-one": ["svc-a-1"], "ns-two": ["svc-b-1"]},
            fail_on=("ns-one", "ns-two"),
        ),
    )

    result = await _sync_service_health_async(db)

    assert result["errors"] == 2, "оба namespace недоступны — оба в ошибках"
    assert result["inserted"] == 0
    assert result["skipped_empty"] == 0, (
        "слепота не должна маскироваться под «нет сигнала»"
    )
    assert db.query(ServiceHealth).count() == 0


@pytest.mark.asyncio
async def test_partial_blindness_keeps_measured_data(db, monkeypatch):
    """Часть окон не измерена — измеренные всё равно пишем.

    Терять данные из-за одного молчащего запроса незачем: сервис получит
    None по неизмеренной метрике, то есть честное «не знаем», а не ноль.
    """
    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc-a", "ns-one")])

    class _PartialVM(_FakeVM):
        async def by_label(self, query: str, label: str):
            from app.providers.measurement import Measurement

            # Молчит только запрос про рестарты; cpu/mem отвечают.
            if "restart" in query:
                return Measurement.unknown("vm_unavailable: synthetic")
            return await super().by_label(query, label)

    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _PartialVM(ns_pods={"ns-one": ["svc-a-1"]}, pod_value=0.7),
    )

    result = await _sync_service_health_async(db)

    assert result["errors"] == 0, "частичный отказ не делает namespace неуспешным"
    assert result["inserted"] == 1
    row = db.query(ServiceHealth).first()
    assert row.cpu_pct is not None
    assert row.restarts_rate is None, "неизмеренное остаётся неизвестным, не нулём"


@pytest.mark.asyncio
async def test_total_outage_is_reported_as_unavailable(db, monkeypatch):
    """Полный отказ VM обязан читаться как UNAVAILABLE, а не EMPTY.

    `status_from_counts` проверяет нулевые observed РАНЬШЕ, чем errors,
    поэтому без маркера полная слепота классифицировалась как EMPTY. А
    EMPTY в self-health намеренно не считается нездоровьем — пустое окно
    бывает штатным, — и тревога не поднималась вовсе.
    """
    from app.knowledge_graph.source_status import SourceStatus, status_from_counts

    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc-a", "ns-one"), ("svc-b", "ns-two")])
    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _FakeVM(
            ns_pods={"ns-one": ["svc-a-1"], "ns-two": ["svc-b-1"]},
            fail_on=("ns-one", "ns-two"),
        ),
    )

    result = await _sync_service_health_async(db)

    assert result.get("skipped"), "нужен маркер недоступности источника"
    status = status_from_counts(
        result, observed_keys=("fetched",), unavailable_keys=("skipped",)
    )
    assert status is SourceStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_partial_outage_is_not_reported_as_unavailable(db, monkeypatch):
    """Один недоступный namespace из двух — это PARTIAL, а не отказ источника."""
    from app.knowledge_graph.source_status import SourceStatus, status_from_counts

    monkeypatch.setattr(settings, "VICTORIA_METRICS_URL", "http://vm:8428")
    _seed_services(db, [("svc-a", "ns-one"), ("svc-b", "ns-two")])
    monkeypatch.setattr(
        metrics_sync, "make_metrics_provider",
        lambda *a, **kw: _FakeVM(
            ns_pods={"ns-one": ["svc-a-1"], "ns-two": ["svc-b-1"]},
            pod_value=0.4, fail_on=("ns-two",),
        ),
    )

    result = await _sync_service_health_async(db)

    assert not result.get("skipped")
    status = status_from_counts(
        result, observed_keys=("fetched",), unavailable_keys=("skipped",)
    )
    assert status is SourceStatus.PARTIAL


def test_failed_orleans_query_does_not_record_zero_failures():
    """Упавший запрос про сбои не должен записаться как «сбоев не было».

    Правило «нет серии при живом latency_count значит ноль» верно только
    когда источник ответил: prometheus-net не экспортирует счётчик до
    первого инкремента. Для упавшего запроса это утверждение неверно.
    """
    acc = {"orleans_latency_count": 100.0, "orleans_latency_sum": 2.0}

    measured = metrics_sync._orleans_metrics(acc, set())
    blind = metrics_sync._orleans_metrics(acc, {"orleans_timedout_rate"})

    assert measured["orleans_timedout_rate"] == 0.0, "тишина источника = ноль сбоев"
    assert blind["orleans_timedout_rate"] is None, "отказ источника ≠ ноль сбоев"
    # Остальные метрики упавший запрос не портит.
    assert blind["orleans_activation_churn"] == 0.0
    assert blind["orleans_latency_avg_ms"] == measured["orleans_latency_avg_ms"]


def test_failed_latency_count_makes_everything_unknown():
    """Без счётчика вызовов ни одно из правил применить нельзя."""
    acc = {"orleans_latency_count": 100.0, "orleans_latency_sum": 2.0}

    blind = metrics_sync._orleans_metrics(acc, {"orleans_latency_count"})

    assert all(v is None for v in blind.values())


def test_failed_latency_sum_does_not_record_zero_latency():
    """Упавший latency_sum не должен дать «латентность нулевая»."""
    acc = {"orleans_latency_count": 100.0, "orleans_latency_sum": 5.0}

    blind = metrics_sync._orleans_metrics(acc, {"orleans_latency_sum"})

    assert blind["orleans_latency_avg_ms"] is None
