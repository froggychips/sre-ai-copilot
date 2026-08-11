"""Тесты адаптивного threshold (robust z + MAD) в anomaly_detection.

См. `app/knowledge_graph/anomaly_detection.py`:
- robust_z = (current - median) / (1.4826 × MAD) — устойчив к outlier-ам.
- Seasonal baseline активируется при ≥ 50 точек, стратификация по hour ±1.
- Volume guard: не более 3 observations per service/metric за окно (1ч).
- Threshold-ы из env KG_ANOMALY_ROBUST_Z_WARN/CRIT (default 3.5 / 6.0).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.anomaly_detection import (LOG_ERROR_METRIC,
                                                   VOLUME_GUARD_MAX_PER_HOUR,
                                                   detect_anomalies)
from app.knowledge_graph.schema import (NODE_KIND_WORKLOAD, AnomalyObservation,
                                        LogObservation, Service, ServiceHealth)


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


def _svc(db, name="svc-a", namespace="squad-1"):
    s = Service(name=name, namespace=namespace, synthetic=False)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _seed_health(db, svc_id, points):
    """points: iterable of (ts, value) — пишет cpu_pct."""
    rows = [
        ServiceHealth(service_id=svc_id, ts=ts, cpu_pct=float(v), source="vm")
        for ts, v in points
    ]
    db.add_all(rows)
    db.commit()


# ---------- tests ---------------------------------------------------------

def test_flat_baseline_small_bump_does_not_trigger(db):
    """Flat baseline (все 50.0), current чуть выше (52.0) → не должно
    сработать. MAD-based устойчив (MAD=0 → метрика skipped)."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)
    # 7 дней по точке в 10 минут, все = 50.0
    baseline_points = []
    t0 = now - timedelta(days=7)
    for i in range(20):  # достаточно >MIN_BASELINE_POINTS=10
        baseline_points.append((t0 + timedelta(minutes=10 * i), 50.0))
    # current window (последний час): 52.0 (маленький bump)
    for i in range(6):
        baseline_points.append(
            (now - timedelta(minutes=10 * (5 - i)), 52.0),
        )
    _seed_health(db, svc.id, baseline_points)

    stats = detect_anomalies(db, now=now)
    # MAD=0 → skipped_no_baseline (мы относим к этому счётчику).
    assert stats["inserted"] == 0


def test_real_spike_triggers_critical(db):
    """Реальный spike (current ≈ 10× median, нормальный baseline-разброс)
    → critical."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)

    # Baseline: ~20 с разбросом ±2 (MAD≈1.4 →  scaled_mad≈2).
    # 22 точки с шагом 4 часа — это окно ~88 часов, влезает в 7d.
    baseline_points = []
    baseline_start = now - timedelta(days=6)
    rng_values = [18.0, 22.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5,
                  18.5, 22.5, 19.8, 20.2, 20.8, 19.2, 21.2, 18.8,
                  20.0, 21.0, 19.5, 20.5, 19.0, 21.5]
    for i, v in enumerate(rng_values):
        baseline_points.append((baseline_start + timedelta(hours=4 * i), v))
    # current: 200.0 (10× baseline median ≈ 20). Точки в последнем часу.
    for i in range(6):
        baseline_points.append(
            (now - timedelta(minutes=10 * (5 - i)), 200.0),
        )
    _seed_health(db, svc.id, baseline_points)

    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] == 1
    # by_severity счётчик использует created_at >= now - 1min, а в тестах
    # `now` фиксирован в прошлом — created_at реальный, поэтому он не
    # попадёт в окно. Проверяем severity напрямую по строке.
    obs = db.query(AnomalyObservation).one()
    assert obs.severity == "critical"
    assert obs.metric == "cpu_pct"
    assert obs.extras is not None
    # Baseline 22 точек < 50 → flat path.
    assert obs.extras["method"] == "robust_z_flat"


def test_workload_twin_does_not_double_anomaly(db):
    """Пара «Service foo + workload foo» (contract 2.4) с идентичными
    ServiceHealth-строками → аномалия рождается ОДИН раз, на Service-узле.

    Регрессия: без фильтра node_kind в detect_anomalies каждая аномалия
    вставлялась дважды (по разу на узел пары) — удвоение шло прямиком в
    порог >500/сутки в self_health.
    """
    svc = _svc(db)
    twin = Service(name=svc.name, namespace=svc.namespace, synthetic=False,
                   node_kind=NODE_KIND_WORKLOAD)
    db.add(twin)
    db.commit()
    db.refresh(twin)

    now = datetime(2026, 5, 23, 12, 0, 0)
    # Тот же spike-сценарий, что в test_real_spike_triggers_critical,
    # продублированный на оба узла (metrics_sync до фикса писал health
    # обоим — воспроизводим состояние БД после миграции).
    points = []
    baseline_start = now - timedelta(days=6)
    rng_values = [18.0, 22.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5,
                  18.5, 22.5, 19.8, 20.2, 20.8, 19.2, 21.2, 18.8,
                  20.0, 21.0, 19.5, 20.5, 19.0, 21.5]
    for i, v in enumerate(rng_values):
        points.append((baseline_start + timedelta(hours=4 * i), v))
    for i in range(6):
        points.append((now - timedelta(minutes=10 * (5 - i)), 200.0))
    _seed_health(db, svc.id, points)
    _seed_health(db, twin.id, points)

    stats = detect_anomalies(db, now=now)
    assert stats["real_services"] == 1
    assert stats["inserted"] == 1
    obs = db.query(AnomalyObservation).one()
    assert obs.service_id == svc.id


def test_volume_guard_caps_observations_per_hour(db):
    """Volume guard: если уже 3 observations за час — 4-й и 5-й не пишутся."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)

    # Засеем 3 уже существующих наблюдения в окне volume guard,
    # чтобы следующий tick попал в guard.
    for i in range(VOLUME_GUARD_MAX_PER_HOUR):
        db.add(AnomalyObservation(
            service_id=svc.id,
            ts=now - timedelta(minutes=30 + i),
            metric="cpu_pct",
            value=100.0,
            baseline_mean=20.0,
            baseline_stddev=2.0,
            z_score=5.0,
            severity="critical",
            notified=False,
            created_at=now - timedelta(minutes=30 + i),
        ))
    db.commit()

    # И ставим текущий spike — он должен попасть в guard.
    baseline_points = []
    baseline_start = now - timedelta(days=6)
    rng_values = [18.0, 22.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5,
                  18.5, 22.5, 19.8, 20.2, 20.8, 19.2]
    for i, v in enumerate(rng_values):
        baseline_points.append((baseline_start + timedelta(hours=4 * i), v))
    # current: огромный spike.
    for i in range(6):
        baseline_points.append(
            (now - timedelta(minutes=10 * (5 - i)), 300.0),
        )
    _seed_health(db, svc.id, baseline_points)

    stats = detect_anomalies(db, now=now)
    # Spike детектируется, но volume guard срабатывает.
    assert stats["inserted"] == 0
    assert stats["skipped_volume_guard"] >= 1


def test_volume_guard_first_three_pass_then_block(db):
    """Запускаем detect 5 раз с разным anomaly_ts (чтобы UNIQUE не помешал),
    spike тот же. Первые 3 должны вставиться, 4-й и 5-й — отрезаны guard'ом."""
    svc = _svc(db)
    base_now = datetime(2026, 5, 23, 12, 0, 0)

    # Baseline: ~20 ± 2.
    baseline_start = base_now - timedelta(days=6)
    rng_values = [18.0, 22.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5,
                  18.5, 22.5, 19.8, 20.2, 20.8, 19.2]
    base_points = [(baseline_start + timedelta(hours=4 * i), v)
                   for i, v in enumerate(rng_values)]
    _seed_health(db, svc.id, base_points)

    # Прогоняем 5 раз — каждый раз с новым current-окном со spike-ом.
    # ts последней current-точки используется как anomaly_ts → UNIQUE.
    inserted_total = 0
    blocked_total = 0
    for tick in range(5):
        now_i = base_now + timedelta(minutes=10 * tick)
        # current window — последние 6 точек заканчиваются в now_i.
        # Чтобы у каждого tick anomaly_ts был уникальным — добавим
        # отдельную точку.
        db.add(ServiceHealth(
            service_id=svc.id,
            ts=now_i - timedelta(minutes=1),  # внутри current_start окна
            cpu_pct=300.0,
            source="vm",
        ))
        db.commit()
        stats_i = detect_anomalies(db, now=now_i)
        inserted_total += stats_i["inserted"]
        blocked_total += stats_i["skipped_volume_guard"]

    assert inserted_total == VOLUME_GUARD_MAX_PER_HOUR
    # 5 - 3 = 2 заблокированных.
    assert blocked_total == 5 - VOLUME_GUARD_MAX_PER_HOUR


def test_thresholds_overridable_via_env(db, monkeypatch):
    """Сделать критический spike очень мягкими порогами — попадаем в critical;
    жёсткими — не сработает."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)

    # baseline ~20 ± 2.
    baseline_start = now - timedelta(days=6)
    rng_values = [18.0, 22.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5,
                  18.5, 22.5, 19.8, 20.2, 20.8, 19.2]
    base_points = [(baseline_start + timedelta(hours=4 * i), v)
                   for i, v in enumerate(rng_values)]
    # current: умеренный bump — ~30. robust_z = (30-20.x)/~2 ≈ 5.
    for i in range(6):
        base_points.append((now - timedelta(minutes=10 * (5 - i)), 30.0))
    _seed_health(db, svc.id, base_points)

    # Жёсткий порог 10/20 — не сработает.
    monkeypatch.setenv("KG_ANOMALY_ROBUST_Z_WARN", "10.0")
    monkeypatch.setenv("KG_ANOMALY_ROBUST_Z_CRIT", "20.0")
    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] == 0


def test_seasonal_baseline_kicks_in_with_enough_data(db):
    """≥50 baseline-точек И ≥10 seasonal-точек → method='robust_z_seasonal'."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 14, 0, 0)  # target_hour = 14

    base_points = []
    # 200 точек с шагом 30 мин = 100 часов ≈ 4 дня. Покрывает разные hour-
    # of-day. Каждый час повторяется ~4 раза, в band {13,14,15} попадёт
    # ≥12 точек — этого хватает для seasonal-пути.
    t0 = now - timedelta(days=5)
    for i in range(200):
        ts = t0 + timedelta(minutes=30 * i)
        # Сезонный паттерн: ночью 20, днём 50, пик 13-15 — 90 ± разброс.
        if 13 <= ts.hour <= 15:
            v = 90.0 + (i % 5)
        elif ts.hour < 8 or ts.hour > 20:
            v = 20.0 + (i % 3)
        else:
            v = 50.0 + (i % 4)
        base_points.append((ts, v))

    # current spike — 300.0
    for i in range(6):
        base_points.append((now - timedelta(minutes=10 * (5 - i)), 300.0))
    _seed_health(db, svc.id, base_points)

    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] >= 1
    obs = db.query(AnomalyObservation).first()
    assert obs.extras is not None
    assert obs.extras["method"] == "robust_z_seasonal"
    assert obs.extras["target_hour"] == 14


def test_baseline_with_outliers_still_detects(db):
    """Robust-z устойчив: 1 outlier в baseline (deploy-spike прошлой недели)
    не отравляет порог, текущий реальный spike всё равно ловится."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)

    base_points = []
    baseline_start = now - timedelta(days=6)
    # 20 точек около 20 + один outlier в 500 (типа прошлый-deploy spike).
    rng_values = [20.0, 19.5, 20.5, 21.0, 19.0, 20.0, 21.5, 18.5,
                  22.5, 19.8, 20.2, 20.8, 19.2, 21.2, 18.8, 20.0,
                  21.0, 19.5, 20.5, 500.0]  # outlier
    for i, v in enumerate(rng_values):
        base_points.append((baseline_start + timedelta(hours=4 * i), v))
    # current: 200.
    for i in range(6):
        base_points.append((now - timedelta(minutes=10 * (5 - i)), 200.0))
    _seed_health(db, svc.id, base_points)

    stats = detect_anomalies(db, now=now)
    # При обычном mean/stddev outlier 500 раздул бы stddev и спрятал
    # current=200. С MAD median≈20, MAD≈1 → robust_z ≈ 100+ → critical.
    assert stats["inserted"] == 1
    obs = db.query(AnomalyObservation).one()
    assert obs.severity == "critical"
    # robust median ~20, не подскочившая до ~30 (как было бы у среднего).
    assert obs.baseline_mean < 25.0


# ---------- noise floor (WO-11335 / KG-polish) ----------------------------

def _near_flat_baseline(now):
    """20 baseline-точек ~6.87 с микро-jitter (MAD≈0.001) + current 8.04.

    Воспроизводит prod-артефакт: mem 6.87→8.04% давал z≈900 (scaled_mad≈0.0015),
    хотя сдвиг всего 1.2 п.п. (~17% относительно).
    """
    pts = []
    start = now - timedelta(days=6)
    for i in range(20):
        pts.append((start + timedelta(hours=4 * i), 6.870 + (0.002 if i % 2 else 0.0)))
    for i in range(6):
        pts.append((now - timedelta(minutes=10 * (5 - i)), 8.04))
    return pts


def test_near_flat_micro_jitter_suppressed_by_floor(db):
    """Near-flat baseline + малый абсолютный сдвиг → noise floor давит z,
    аномалия НЕ пишется (раньше был z≈900 critical)."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)
    _seed_health(db, svc.id, _near_flat_baseline(now))
    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] == 0


def test_without_rel_floor_same_bump_would_trigger(db, monkeypatch):
    """Тот же near-flat кейс при выключенном rel-floor (=0) → срабатывает.
    Доказывает, что давит именно относительный пол, а не порог z."""
    monkeypatch.setenv("KG_ANOMALY_REL_SPREAD_FLOOR", "0")
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)
    _seed_health(db, svc.id, _near_flat_baseline(now))
    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] == 1


def test_flat_zero_mad_large_jump_now_detected(db):
    """Сервис, годами стоявший ровно (MAD=0), при РЕАЛЬНОМ крупном скачке
    теперь детектируется через abs/rel floor (раньше MAD=0 молча скипал)."""
    svc = _svc(db)
    now = datetime(2026, 5, 23, 12, 0, 0)
    pts = []
    start = now - timedelta(days=6)
    for i in range(20):
        pts.append((start + timedelta(hours=4 * i), 50.0))  # абсолютно плоско
    for i in range(6):
        pts.append((now - timedelta(minutes=10 * (5 - i)), 200.0))  # x4 скачок
    _seed_health(db, svc.id, pts)
    stats = detect_anomalies(db, now=now)
    assert stats["inserted"] == 1
    obs = db.query(AnomalyObservation).one()
    assert obs.severity == "critical"
    # baseline_stddev = effective_spread = max(0, 0.10×50, 0.02) = 5.0; z=(200-50)/5=30.
    assert obs.baseline_stddev == pytest.approx(5.0, rel=1e-3)
    assert abs(obs.z_score) == pytest.approx(30.0, rel=1e-2)


# ---------- log-error anomaly (kg_log_observations consumer) --------------

def _seed_log_errors(db, svc_id, points, level="Error"):
    """points: iterable of (ts, count) — пишет Error-логи в kg_log_observations."""
    db.add_all([
        LogObservation(service_id=svc_id, ts=ts, level=level, count=int(c),
                       source="seq")
        for ts, c in points
    ])
    db.commit()


def test_log_error_spike_triggers_anomaly(db):
    """Всплеск Error-логов относительно типичного объёма → log_error_rate-аномалия."""
    now = datetime(2026, 6, 6, 12, 0, 0)
    svc = _svc(db, name="map-service", namespace="prod-kingdom5")
    # baseline: ~2 дня варьирующихся мелких error-бакетов (MAD>0), все < now-1h
    base = []
    for i in range(60):
        ts = now - timedelta(hours=2) - timedelta(minutes=30 * i)
        base.append((ts, 1 + (i % 4)))  # 1..4 ошибок/бакет
    _seed_log_errors(db, svc.id, base)
    # current: последний час — резкий всплеск
    cur = [(now - timedelta(minutes=10 * k), 40) for k in range(3)]
    _seed_log_errors(db, svc.id, cur)

    stats = detect_anomalies(db, now=now)

    obs = db.query(AnomalyObservation).filter(
        AnomalyObservation.metric == LOG_ERROR_METRIC).all()
    assert len(obs) == 1
    assert obs[0].z_score > 0            # только рост
    assert obs[0].extras["is_log_proxy"] is True
    assert stats["log_error_services"] >= 1


def test_log_error_no_spike_no_anomaly(db):
    """Стабильный мелкий объём ошибок без всплеска → нет log_error_rate-аномалии."""
    now = datetime(2026, 6, 6, 12, 0, 0)
    svc = _svc(db, name="quiet-svc", namespace="prod-shared")
    pts = []
    for i in range(70):
        ts = now - timedelta(minutes=20 * i)
        pts.append((ts, 1 + (i % 3)))   # 1..3, без всплеска
    _seed_log_errors(db, svc.id, pts)

    detect_anomalies(db, now=now)

    obs = db.query(AnomalyObservation).filter(
        AnomalyObservation.metric == LOG_ERROR_METRIC).all()
    assert obs == []
