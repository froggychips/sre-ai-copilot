"""Пакетная загрузка точек в детекторе аномалий эквивалентна построчной.

До 18.11.2026 `detect_anomalies` делал SELECT семи дней `kg_service_health`
на каждый сервис (первое место в pg_stat_statements по суммарному времени).
Теперь точки грузятся пачками. Тест держит две вещи:

* результат (строки `kg_anomaly_observations` и счётчики) тот же, что у
  построчного пути `_detect_for_service` на той же фикстуре;
* число запросов к `kg_service_health` не растёт с числом сервисов.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import anomaly_detection as ad
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, AnomalyObservation,
                                        Service, ServiceHealth)

NOW = datetime(2026, 11, 18, 12, 0, 0)
COUNTERS = ("inserted", "skipped_dup", "skipped_no_baseline",
            "skipped_no_current", "skipped_volume_guard")


def _make_db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _seed(db: Any, n_services: int = 37) -> None:
    """Сервисы всех видов: со всплеском, ровные, без свежих точек, без точек.

    Генератор с фиксированным зерном — две базы получают одно и то же.
    """
    rng = random.Random(20261118)
    for i in range(n_services):
        svc = Service(
            name=f"svc-{i}", namespace=f"squad-{i % 5}",
            synthetic=False, node_kind=NODE_KIND_SERVICE,
        )
        db.add(svc)
        db.flush()
        kind = i % 6
        if kind == 5:
            continue                      # точек нет вовсе
        rows: List[ServiceHealth] = []
        # baseline: шаг 2 ч на 6 суток (72 точки → включается seasonal)
        # либо редкий (12 точек → flat), у части сервисов — дыры в метриках.
        step_h = 2 if kind in (0, 1, 4) else 12
        t = NOW - timedelta(days=6, hours=23)
        while t < NOW - timedelta(hours=1, minutes=5):
            rows.append(ServiceHealth(
                service_id=svc.id, ts=t, source="vm",
                cpu_pct=rng.uniform(0.1, 0.5),
                mem_pct=rng.uniform(40.0, 45.0) if kind != 2 else None,
                restarts_rate=0.0,
                orleans_latency_avg_ms=(
                    rng.uniform(900.0, 1100.0) if kind == 1 else None
                ),
            ))
            t += timedelta(hours=step_h)
        if kind != 4:                     # kind 4: история есть, свежих нет
            spike = kind in (0, 1)
            for k in range(rng.randint(1, 8)):
                rows.append(ServiceHealth(
                    service_id=svc.id, ts=NOW - timedelta(minutes=7 * k),
                    source="vm",
                    cpu_pct=rng.uniform(5.0, 9.0) if spike else rng.uniform(0.1, 0.5),
                    mem_pct=rng.uniform(40.0, 45.0),
                    restarts_rate=3.0 if spike and i % 2 else 0.0,
                    orleans_latency_avg_ms=9000.0 if kind == 1 else None,
                ))
        db.add_all(rows)
    db.commit()


def _observations(db: Any) -> List[Tuple[Any, ...]]:
    return [
        (o.service_id, o.ts, o.metric, o.value, o.baseline_mean,
         o.baseline_stddev, o.z_score, o.severity, o.notified, o.extras)
        for o in db.query(AnomalyObservation)
        .order_by(AnomalyObservation.service_id, AnomalyObservation.metric,
                  AnomalyObservation.ts)
        .all()
    ]


def _run_rowwise(db: Any) -> Dict[str, int]:
    """Прежний путь: SELECT на сервис, тот же порядок и те же пороги."""
    total = dict.fromkeys(COUNTERS, 0)
    services = (
        db.query(Service)
        .filter(Service.synthetic.is_(False),
                Service.node_kind == NODE_KIND_SERVICE)
        .all()
    )
    for svc in services:
        counters = ad._detect_for_service(
            db, svc, NOW,
            warn_thresh=ad._threshold_warn(), crit_thresh=ad._threshold_crit(),
            rel_floor=ad._rel_spread_floor(),
        )
        for k in COUNTERS:
            total[k] += counters[k]
    db.commit()
    return total


@pytest.mark.parametrize("chunk", [1, 7, 50])
def test_batch_path_matches_rowwise(monkeypatch: pytest.MonkeyPatch, chunk: int) -> None:
    monkeypatch.setattr(ad, "_HEALTH_FETCH_CHUNK", chunk)
    monkeypatch.setattr(ad, "_ID_CHUNK", max(chunk, 3))
    old_db, new_db = _make_db(), _make_db()
    try:
        _seed(old_db)
        _seed(new_db)

        old_counters = _run_rowwise(old_db)
        stats = ad.detect_anomalies(new_db, now=NOW)

        old_obs, new_obs = _observations(old_db), _observations(new_db)
        assert old_obs, "фикстура должна давать аномалии, иначе сравнивать нечего"
        # Бит-в-бит: значения float сравниваются на равенство, не approx.
        assert new_obs == old_obs
        assert {k: stats[k] for k in COUNTERS} == old_counters
        assert stats["errors"] == 0
        # В фикстуре есть оба «пустых» случая — иначе счётчик не проверен.
        assert old_counters["skipped_no_current"] >= len(ad.METRICS)
    finally:
        old_db.close()
        new_db.close()


def test_health_queries_do_not_grow_with_services() -> None:
    """Запросов к kg_service_health — на пачку, а не на сервис."""
    db = _make_db()
    try:
        _seed(db, n_services=60)
        statements: List[str] = []

        def _before(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
            statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _before)
        try:
            ad.detect_anomalies(db, now=NOW)
        finally:
            event.remove(engine, "before_cursor_execute", _before)

        health_selects = [
            s for s in statements
            if s.lstrip().upper().startswith("SELECT") and "kg_service_health" in s
        ]
        # 60 сервисов: 1 запрос «у кого свежие точки» + 1 EXISTS-пачка +
        # 1 пачка выгрузки (сервисов со свежими точками 40 < 50). Раньше — 60.
        assert len(health_selects) == 3, health_selects
    finally:
        db.close()
