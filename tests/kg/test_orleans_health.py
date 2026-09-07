"""Здоровье Orleans-силоса: агрегация metrics_sync, запрос для embed, рендер, детектор."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import metrics_sync as ms
from app.knowledge_graph.anomaly_detection import METRICS, MIN_ABS_SPREAD_BY_METRIC
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.queries import ORLEANS_METRICS, orleans_health_for
from app.knowledge_graph.schema import ServiceHealth
from app.services.discord.embed_builder import _build_orleans_field

T0 = datetime(2026, 9, 7, 10, 0, 0)
SVC = [(1, "town-grainhost"), (2, "town-service")]


# ── агрегация ─────────────────────────────────────────────────────────────

def _raw(**orl):
    base = {"cpu_pct": {"town-grainhost-a": 10.0, "town-grainhost-b": 30.0, "town-service-x": 5.0},
            "mem_pct": {}, "restarts_rate": {}, "http_5xx_rate": {}, "p95_latency_ms": {}}
    base.update(orl)
    return base


def test_latency_is_call_weighted_across_pods_and_rates_are_summed():
    raw = _raw(
        orleans_latency_sum={"town-grainhost-a": 7.0, "town-grainhost-b": 1.0},     # с/с
        orleans_latency_count={"town-grainhost-a": 1.0, "town-grainhost-b": 3.0},   # вызовов/с
        orleans_timedout_rate={"town-grainhost-a": 0.5, "town-grainhost-b": 0.25},
        orleans_messaging_fault_rate={"town-grainhost-a": 2.0},
        orleans_activation_churn={"town-grainhost-a": 400.0, "town-grainhost-b": 380.0},
    )
    out = {name: m for _sid, name, m in ms._aggregate_service_metrics(raw, SVC)}
    g = out["town-grainhost"]
    assert g["orleans_latency_avg_ms"] == 2000.0            # (7+1)/(1+3) с → 2000 мс, не avg(7000, 333)
    assert g["orleans_timedout_rate"] == 0.75
    assert g["orleans_messaging_fault_rate"] == 2.0
    assert g["orleans_pings_missed_rate"] == 0.0            # серии нет, силос есть → 0, не None
    assert g["orleans_activation_churn"] == 780.0
    assert g["cpu_pct"] == 20.0                             # старые метрики не задеты


def test_service_without_silo_gets_none_not_zero():
    raw = _raw(orleans_latency_sum={"town-grainhost-a": 7.0}, orleans_latency_count={"town-grainhost-a": 1.0})
    out = {name: m for _sid, name, m in ms._aggregate_service_metrics(raw, SVC)}
    assert all(out["town-service"][m] is None for m in ORLEANS_METRICS)
    assert out["town-grainhost"]["orleans_latency_avg_ms"] == 7000.0


def test_no_orleans_keys_at_all_is_backward_compatible():
    out = {name: m for _sid, name, m in ms._aggregate_service_metrics(_raw(), SVC)}
    assert all(out["town-grainhost"][m] is None for m in ORLEANS_METRICS)
    assert ms._has_any_signal(out["town-grainhost"])       # cpu есть — строка пишется


def test_orleans_metrics_helper_guards_zero_count():
    assert ms._orleans_metrics({"orleans_latency_sum": 1.0, "orleans_latency_count": 0.0})["orleans_latency_avg_ms"] is None
    assert ms._orleans_metrics(None)["orleans_timedout_rate"] is None


# ── fetch: Orleans-запросы только для namespace'ов из discovery ──────────

class _VM:
    def __init__(self):
        self.queries: List[str] = []

    async def query_instant_by(self, query: str, by_label: str) -> Dict[str, float]:
        self.queries.append(query)
        if by_label == "namespace":
            return {"preprod-kingdom2": 3.0, "squad-6-kingdom2": 1.0}
        if "microsoft_orleans" in query:
            return {"town-grainhost-a": 1.0}
        return {}


def test_fetch_namespace_adds_orleans_queries_only_when_asked():
    vm = _VM()
    sem = asyncio.Semaphore(4)
    _ns, raw, exc = asyncio.run(ms._fetch_namespace(sem, vm, "preprod-kingdom2", orleans=True))
    assert exc is None and raw is not None
    assert {k for k in raw if k.startswith("orleans_")} == {
        "orleans_latency_sum", "orleans_latency_count", "orleans_timedout_rate",
        "orleans_messaging_fault_rate", "orleans_pings_missed_rate", "orleans_activation_churn",
    }
    assert sum("microsoft_orleans" in q for q in vm.queries) == ms.ORLEANS_QUERY_COUNT
    vm2 = _VM()
    _ns, raw2, _ = asyncio.run(ms._fetch_namespace(sem, vm2, "prod-shared", orleans=False))
    assert not any(k.startswith("orleans_") for k in raw2)
    assert not any("microsoft_orleans" in q for q in vm2.queries)


def test_promql_uses_regex_sums_and_per_minute_rates():
    q = ms._q_ns_orleans_faults_by_pod("preprod-kingdom2")
    assert '__name__=~"microsoft_orleans_orleans_messaging_(rerouted|rejected|expired|sent_failed|sent_dropped)"' in q
    assert q.endswith("* 60")
    assert "catalog_activation_(created|destroyed|shutdown)" in ms._q_ns_orleans_churn_by_pod("x")
    assert ms._q_orleans_namespaces() == "count by (namespace) (microsoft_orleans_orleans_app_requests_latency_count)"


# ── детектор аномалий знает новые метрики ────────────────────────────────

def test_anomaly_detector_registers_orleans_metrics_with_floors():
    for m in ORLEANS_METRICS:
        assert m in METRICS
        assert MIN_ABS_SPREAD_BY_METRIC[m] > 0
    assert MIN_ABS_SPREAD_BY_METRIC["orleans_latency_avg_ms"] == 50.0


# ── запрос для embed + рендер ────────────────────────────────────────────

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _row(sid, ts, latency, timeouts=0.0, faults=0.0, pings=0.0, churn=800.0):
    return ServiceHealth(service_id=sid, ts=ts, cpu_pct=20.0, orleans_latency_avg_ms=latency,
                         orleans_timedout_rate=timeouts, orleans_messaging_fault_rate=faults,
                         orleans_pings_missed_rate=pings, orleans_activation_churn=churn, source="vm")


def test_orleans_health_for_returns_latest_baseline_and_deltas(db):
    svc = upsert_service(db, namespace="preprod-kingdom2", name="town-grainhost")
    db.flush()
    for i in range(1, 7):                                   # база: 6 точек по 6000 мс за сутки
        db.add(_row(svc.id, T0 - timedelta(hours=i), 6000.0, faults=0.0, churn=800.0))
    db.add(_row(svc.id, T0 - timedelta(minutes=5), 9000.0, faults=4.0, churn=1200.0))   # последняя: +50%
    db.commit()
    out = orleans_health_for(db, "preprod-kingdom2", "town-grainhost", now=T0)
    assert out["present"] is True
    assert out["latest"]["orleans_latency_avg_ms"] == 9000.0
    assert out["baseline"]["orleans_latency_avg_ms"] == 6000.0
    assert out["deltas_pct"]["orleans_latency_avg_ms"] == 50.0
    assert out["deltas_pct"]["orleans_activation_churn"] == 50.0
    assert out["deltas_pct"]["orleans_messaging_fault_rate"] is None    # база 0 → дельту не считаем


def test_orleans_health_for_absent_when_no_silo_or_stale(db):
    svc = upsert_service(db, namespace="prod-shared", name="auth")
    db.flush()
    db.add(ServiceHealth(service_id=svc.id, ts=T0 - timedelta(minutes=5), cpu_pct=1.0, source="vm"))
    g = upsert_service(db, namespace="prod-kingdom1", name="town-grainhost")
    db.flush()
    db.add(_row(g.id, T0 - timedelta(hours=3), 5000.0))                # старее окна 60 мин
    db.commit()
    assert orleans_health_for(db, "prod-shared", "auth", now=T0)["present"] is False
    assert orleans_health_for(db, "prod-kingdom1", "town-grainhost", now=T0)["present"] is False
    assert orleans_health_for(db, "ghost", "nobody", now=T0)["present"] is False


def test_embed_field_marks_growth_over_50_percent_and_skips_absent():
    assert _build_orleans_field(None) is None
    assert _build_orleans_field({"present": False}) is None
    field = _build_orleans_field({
        "present": True,
        "latest": {"orleans_latency_avg_ms": 9000.0, "orleans_timedout_rate": 0.0,
                   "orleans_messaging_fault_rate": 4.0, "orleans_pings_missed_rate": 0.0,
                   "orleans_activation_churn": 1200.0},
        "baseline": {"orleans_latency_avg_ms": 6000.0, "orleans_timedout_rate": 0.0,
                     "orleans_messaging_fault_rate": 0.0, "orleans_pings_missed_rate": 0.0,
                     "orleans_activation_churn": 800.0},
        "deltas_pct": {"orleans_latency_avg_ms": 50.0, "orleans_timedout_rate": None,
                       "orleans_messaging_fault_rate": None, "orleans_pings_missed_rate": None,
                       "orleans_activation_churn": 50.0},
    })
    assert field["name"].startswith("🧬 Orleans silo")
    v = field["value"]
    assert "latency avg 9.00 с (24ч 6.00)" in v and "⚠" not in v.split("·")[0]   # ровно +50% — не помечаем
    assert "msg faults 4.0/мин (24ч 0.0)" in v
    assert "activation churn 1200/мин (24ч 800)" in v
    hot = _build_orleans_field({"present": True, "latest": {"orleans_latency_avg_ms": 12000.0},
                                "baseline": {"orleans_latency_avg_ms": 6000.0},
                                "deltas_pct": {"orleans_latency_avg_ms": 100.0}})
    assert "⚠" in hot["value"]
