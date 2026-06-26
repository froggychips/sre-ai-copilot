"""Тесты ingress-derived 5xx-штрафа в compute_health_for_service."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_score import compute_health_for_service
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


def _obs(db, svc, e5xx, p95=100.0, ts=None):
    o = IngressObservation(
        ts=ts or _NOW, ingress_name="ing", host="auth.x.com", path="/login",
        service_id=svc.id, p95_latency_ms=p95, error_5xx_rate=e5xx, rps=10.0,
    )
    db.add(o)
    db.flush()
    return o


def test_no_ingress_data_no_penalty_signals_none(db):
    s = _svc(db)
    db.commit()
    score, signals = compute_health_for_service(db, s, now=_NOW)
    assert score == 1.0
    assert signals["ingress_5xx_rate"] is None
    assert signals["ingress_p95_ms"] is None


def test_high_ingress_5xx_lowers_score(db):
    s = _svc(db)
    _obs(db, s, e5xx=0.55)  # сильно выше триггера 0.05 rps
    db.commit()
    score, signals = compute_health_for_service(db, s, now=_NOW)
    assert score < 1.0
    assert signals["ingress_5xx_rate"] == 0.55
    assert signals["ingress_p95_ms"] == 100.0


def test_low_ingress_5xx_below_trigger_no_penalty(db):
    s = _svc(db)
    _obs(db, s, e5xx=0.01)  # ниже триггера 0.05 → без штрафа
    db.commit()
    score, signals = compute_health_for_service(db, s, now=_NOW)
    assert score == 1.0
    assert signals["ingress_5xx_rate"] == 0.01


def test_ingress_penalty_capped(db):
    s = _svc(db)
    _obs(db, s, e5xx=1000.0)  # экстрим → штраф упирается в cap 0.25
    db.commit()
    score, _ = compute_health_for_service(db, s, now=_NOW)
    assert score == pytest.approx(0.75)  # ровно 1.0 - cap, прочих сигналов нет


def test_stale_ingress_excluded(db):
    s = _svc(db)
    _obs(db, s, e5xx=0.55, ts=_NOW - timedelta(minutes=120))  # вне окна 30м
    db.commit()
    score, signals = compute_health_for_service(db, s, now=_NOW)
    assert score == 1.0
    assert signals["ingress_5xx_rate"] is None
