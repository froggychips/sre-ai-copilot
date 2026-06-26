"""Тесты queries.ingress_health_for — per-service ingress-derived HTTP RED."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.queries import ingress_health_for
from app.knowledge_graph.schema import IngressObservation, Service

_NOW = datetime(2026, 6, 26, 12, 0, 0)


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


def _svc(db, name="auth", ns="prod-shared"):
    s = Service(name=name, namespace=ns)
    db.add(s)
    db.flush()
    return s


def _obs(db, svc, host, path, ts, p95=None, p99=None, rps=None,
         e5xx=None, e4xx=None):
    o = IngressObservation(
        ts=ts, ingress_name="ing", host=host, path=path, service_id=svc.id,
        p95_latency_ms=p95, p99_latency_ms=p99, rps=rps,
        error_5xx_rate=e5xx, error_4xx_rate=e4xx,
    )
    db.add(o)
    db.flush()
    return o


def test_service_not_in_graph_returns_empty(db):
    assert ingress_health_for(db, "prod-shared", "ghost", now=_NOW) == {}


def test_no_observations_returns_zeroed_dict(db):
    _svc(db)
    db.commit()
    r = ingress_health_for(db, "prod-shared", "auth", now=_NOW)
    assert r["endpoints_total"] == 0
    assert r["max_5xx_rate"] == 0.0
    assert r["top_endpoints"] == []
    assert r["is_ingress_derived"] is True


def test_aggregates_peak_and_sum_across_endpoints(db):
    s = _svc(db)
    _obs(db, s, "auth.x.com", "/login", _NOW, p95=300, p99=500, rps=10, e5xx=0.2)
    _obs(db, s, "auth.x.com", "/token", _NOW, p95=120, p99=200, rps=5, e5xx=0.05)
    db.commit()
    r = ingress_health_for(db, "prod-shared", "auth", now=_NOW)
    assert r["endpoints_total"] == 2
    assert r["max_5xx_rate"] == 0.2          # пик
    assert r["max_p95_ms"] == 300.0
    assert r["total_rps"] == 15.0            # сумма
    # top отсортирован по 5xx desc → /login первым
    assert r["top_endpoints"][0]["path"] == "/login"


def test_latest_observation_per_endpoint_wins(db):
    s = _svc(db)
    _obs(db, s, "auth.x.com", "/login", _NOW - timedelta(minutes=10), e5xx=5.0)
    _obs(db, s, "auth.x.com", "/login", _NOW - timedelta(minutes=1), e5xx=0.1)
    db.commit()
    r = ingress_health_for(db, "prod-shared", "auth", now=_NOW)
    # одна точка endpoint'а (последняя), старая 5.0 не учитывается
    assert r["endpoints_total"] == 1
    assert r["max_5xx_rate"] == 0.1


def test_observations_outside_window_excluded(db):
    s = _svc(db)
    _obs(db, s, "auth.x.com", "/login", _NOW - timedelta(minutes=60), e5xx=9.0)
    db.commit()
    r = ingress_health_for(db, "prod-shared", "auth", window_minutes=15, now=_NOW)
    assert r["endpoints_total"] == 0
    assert r["max_5xx_rate"] == 0.0
