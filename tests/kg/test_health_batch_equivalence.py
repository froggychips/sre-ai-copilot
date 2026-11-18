"""Пакетный расчёт health_score эквивалентен построчному.

`recompute_all_health` до 18.11.2026 делал ~8 запросов на сервис: шесть
SELECT сигналов и UPDATE (8,5 млн UPDATE kg_services за 18 дней). Теперь
сигналы собираются на пачку, запись — пакетным UPDATE. Тест держит:

* `compute_health_for_services` даёт те же score и signals, что
  `compute_health_for_service` на той же фикстуре (float — на равенство);
* `recompute_all_health` пишет в базу те же значения;
* число запросов на проход не растёт с числом сервисов.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, List

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import health_score as hs
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, AlertEvent,
                                        IngressObservation, PodEvent, Service,
                                        ServiceHealth, SignalAggregate)

NOW = datetime(2026, 11, 18, 12, 0, 0)


@pytest.fixture
def db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed(db: Any, n_services: int = 45) -> List[Service]:
    """Сервисы со всеми сочетаниями сигналов; зерно фиксировано."""
    rng = random.Random(1811)
    services: List[Service] = []
    for i in range(n_services):
        svc = Service(name=f"svc-{i}", namespace=f"prod-kingdom{i % 4}",
                      synthetic=False, node_kind=NODE_KIND_SERVICE)
        db.add(svc)
        db.flush()
        services.append(svc)

        # Алерты: открытые разной важности и регистра, закрытые, фантом >30д.
        for k in range(rng.randint(0, 7)):
            db.add(AlertEvent(
                service_id=svc.id, alertname=f"A{k}",
                severity=rng.choice(["critical", "Warning", "warning", "info", None]),
                fingerprint=f"fp-{i}-{k}",
                fired_at=NOW - timedelta(hours=rng.choice([1, 5, 20, 30, 90, 24 * 40])),
                resolved_at=(NOW - timedelta(minutes=30)) if rng.random() < 0.4 else None,
            ))
        # Pod events: хронические и нет, last_seen NULL, вне окна.
        for k in range(rng.randint(0, 3)):
            db.add(PodEvent(
                service_id=svc.id, namespace=svc.namespace, pod_name=f"p-{i}-{k}",
                reason="BackOff", event_uid=f"uid-{i}-{k}",
                first_seen=NOW - timedelta(days=rng.choice([1, 5, 9])),
                last_seen=rng.choice([None, NOW - timedelta(days=1),
                                      NOW - timedelta(days=10)]),
                count=rng.choice([10, 1000, 1001, 11789]),
            ))
        # Метрики: у части — меньше порога точек, у части p95/5xx NULL.
        mode = i % 5
        if mode != 0:
            n_recent = 3 if mode == 1 else rng.randint(6, 9)
            for k in range(n_recent):
                db.add(ServiceHealth(
                    service_id=svc.id, ts=NOW - timedelta(minutes=6 * k), source="vm",
                    cpu_pct=0.2,
                    p95_latency_ms=None if mode == 2 else rng.uniform(100.0, 900.0),
                    http_5xx_rate=None if mode == 2 else rng.uniform(0.0, 0.4),
                ))
            if mode in (3, 4):
                for k in range(40):
                    db.add(ServiceHealth(
                        service_id=svc.id, source="vm",
                        ts=NOW - timedelta(hours=2 + 4 * k),
                        p95_latency_ms=None if k % 7 == 0 else rng.uniform(100.0, 300.0),
                    ))
        # Ingress: несколько endpoint'ов, по несколько записей во времени.
        if i % 3 == 0:
            for ep in range(rng.randint(1, 3)):
                for k in range(3):
                    db.add(IngressObservation(
                        ts=NOW - timedelta(minutes=5 + 7 * k + ep),
                        ingress_name=f"ing-{i}", host=f"h{i}.example.com",
                        path=f"/p{ep}", service_id=svc.id, rps=10.0,
                        p95_latency_ms=rng.choice([None, rng.uniform(50.0, 500.0)]),
                        error_5xx_rate=rng.choice([None, 0.01, rng.uniform(0.0, 0.5)]),
                    ))
        # Агрегаты: свежие (берётся последний), протухший, отсутствующие.
        if i % 4 != 0:
            for h in ([1, 2] if i % 4 == 1 else [5] if i % 4 == 2 else [0, 2, 8]):
                db.add(SignalAggregate(
                    service_id=svc.id, window_end=NOW - timedelta(hours=h),
                    window_hours=24,
                    deploy_failure_pct=rng.choice([None, 5.0, rng.uniform(20.0, 90.0)]),
                    slo_burn_pct=rng.choice([None, 3.0, rng.uniform(10.0, 60.0)]),
                ))
    db.commit()
    return services


def test_batch_matches_rowwise(db: Any) -> None:
    services = _seed(db)
    rowwise = {
        s.id: hs.compute_health_for_service(db, s, now=NOW) for s in services
    }
    for size in (1, 7, 100):
        batch = {}
        for start in range(0, len(services), size):
            chunk = services[start:start + size]
            batch.update(hs.compute_health_for_services(
                db, [(s.id, s.namespace, s.name) for s in chunk], now=NOW,
            ))
        # Бит-в-бит: словари сравниваются на равенство, без approx.
        assert batch == rowwise, f"пачка {size}"

    scores = {score for score, _ in rowwise.values()}
    assert len(scores) > 5, "фикстура должна давать разные score"
    assert any(sig["p95_drift_pct"] is not None for _, sig in rowwise.values())
    assert any(sig["ingress_5xx_rate"] is not None for _, sig in rowwise.values())
    assert any(sig["deploy_failure_pct"] is not None for _, sig in rowwise.values())
    assert any(sig["chronic_pod_events"] for _, sig in rowwise.values())


def test_recompute_writes_rowwise_scores(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    services = _seed(db)
    # Узел вне пересчёта: synthetic — его score трогать нельзя.
    skipped = Service(name="statics", namespace="prod-kingdom0", synthetic=True)
    db.add(skipped)
    db.commit()

    class _FrozenNow(datetime):
        @classmethod
        def utcnow(cls) -> datetime:  # type: ignore[override]
            return NOW

    monkeypatch.setattr(hs, "datetime", _FrozenNow)
    expected = {
        s.id: hs.compute_health_for_service(db, s, now=NOW)[0] for s in services
    }
    before_updated = {s.id: s.updated_at for s in services}

    stats = hs.recompute_all_health(db)

    db.expire_all()
    got = {s.id: s.health_score for s in db.query(Service).filter(
        Service.synthetic.is_(False)).all()}
    assert got == expected
    assert stats["recomputed"] == stats["real_services"] == len(services)
    assert stats["low_health"] == sum(1 for v in expected.values() if v < 0.4)
    assert stats["perfect_health"] == sum(1 for v in expected.values() if v >= 0.99)
    for s in db.query(Service).filter(Service.synthetic.is_(False)).all():
        assert s.health_computed_at == NOW
        # onupdate у updated_at должен сработать, как при ORM-записи: на него
        # опирается populator (SERVICE_TOUCH_INTERVAL) и self-health.
        assert s.updated_at is not None and s.updated_at > before_updated[s.id]
    db.refresh(skipped)
    assert skipped.health_score is None and skipped.health_computed_at is None


def test_recompute_query_count_is_per_batch(db: Any) -> None:
    """Запросов — на пачку из 100 сервисов, а не на сервис."""
    def _count(n_services: int) -> int:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
        try:
            _seed(session, n_services=n_services)
            statements: List[str] = []

            def _before(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", _before)
            hs.recompute_all_health(session)
            event.remove(engine, "before_cursor_execute", _before)
            return len(statements)
        finally:
            session.close()
            engine.dispose()

    # Одна пачка в обоих случаях: список сервисов, пропавшие namespace,
    # шесть-семь SELECT сигналов и один UPDATE. Раньше — ~8 на сервис.
    small, large = _count(20), _count(90)
    assert small == large, (small, large)
    assert large <= 10, large
