"""Тесты на confidence-score в deploy_correlator.

Логика тарировки (см. app/rca/deploy_correlator.py):
- N_spikes / max_zscore — сколько/насколько сильно метрики выросли.
- time_proximity = exp(-Δt_min / 30) — чем ближе deploy, тем сильнее сигнал.
- status_factor = 1.2 для FAILURE, 1.0 для SUCCESS.
- flat_baseline_penalty: stddev≈0 у всех метрик → confidence × 0.3.
- verdict-тиры: likely≥0.7 / suspect 0.4-0.7 / weak 0.2-0.4 / unlikely <0.2.

Используем in-memory SQLite + Base.metadata, как остальные KG-тесты.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import Deployment, Service, ServiceHealth
from app.rca.deploy_correlator import correlate_deploy_to_incident


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


# ---------- helpers -------------------------------------------------------

def _make_service(db, name="svc-a", namespace="squad-1"):
    svc = Service(name=name, namespace=namespace, synthetic=False)
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def _make_deploy(db, svc_id, started_at, status="SUCCESS"):
    d = Deployment(
        service_id=svc_id,
        sha="abc1234",
        repo="group/repo",
        buildtype_id="BT_Test",
        build_number="42",
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=2),
        status=status,
        triggered_by="ci",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _health(svc_id, ts, **metrics):
    """Build a ServiceHealth row with optional metrics."""
    return ServiceHealth(service_id=svc_id, ts=ts, source="vm", **metrics)


def _seed_health_window(db, svc_id, deploy_ts, before_values, after_values,
                        metric="cpu_pct", step_minutes=10):
    """Создаёт точки до/после deploy с заданными значениями метрики.

    before_values: list[float] — значения для точек ts < deploy_ts.
    after_values:  list[float] — значения для точек ts > deploy_ts.
    """
    # Расставляем точки равномерно во временных окнах ±60 минут.
    rows = []
    for i, v in enumerate(reversed(before_values)):
        # последняя before-точка — ближе к deploy_ts.
        ts = deploy_ts - timedelta(minutes=step_minutes * (i + 1))
        rows.append(_health(svc_id, ts, **{metric: float(v)}))
    for i, v in enumerate(after_values):
        ts = deploy_ts + timedelta(minutes=step_minutes * (i + 1))
        rows.append(_health(svc_id, ts, **{metric: float(v)}))
    db.add_all(rows)
    db.commit()


# ---------- tests ---------------------------------------------------------

def test_no_recent_deploy_returns_none(db):
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    # Никаких deploy-ов не создаём.
    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    assert res["deploy"] is None
    assert res["reason"] == "no_recent_deploy"


def test_one_spike_failure_close_to_incident(db):
    """1 spike метрика, FAILURE deploy, Δt=5min, normal stddev →
    confidence ≥ ~0.4, verdict suspect/likely."""
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=5)
    _make_deploy(db, svc.id, deploy_ts, status="FAILURE")

    # cpu_pct: до — нормальное распределение около 20, после — 80 (4× выше).
    # stddev перед deploy ~1.5 → z будет огромный, но мы клипуем z_factor.
    _seed_health_window(
        db, svc.id, deploy_ts,
        before_values=[20.0, 21.0, 19.5, 22.0, 20.5, 19.8],
        after_values=[80.0, 82.0, 79.0, 81.0],
        metric="cpu_pct",
    )

    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    assert res["deploy"]["status"] == "FAILURE"
    assert res["n_spikes"] >= 1
    # Δt=5min → time_proximity ~exp(-5/30) ≈ 0.85
    assert res["scoring"]["time_proximity"] > 0.8
    # FAILURE-bonus и большой z → confidence заметная.
    assert res["confidence"] >= 0.4, (
        f"expected confidence ≥ 0.4 for FAILURE + 1 spike + close, got {res}"
    )
    assert res["verdict"] in ("suspect", "likely")


def test_no_spikes_yields_unlikely(db):
    """0 spike метрик (после deploy всё в норме) → confidence низкая."""
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=20)
    _make_deploy(db, svc.id, deploy_ts, status="SUCCESS")

    # cpu_pct: до и после одинаковые ~20 (±1).
    _seed_health_window(
        db, svc.id, deploy_ts,
        before_values=[20.0, 21.0, 19.5, 20.5, 20.0, 19.8],
        after_values=[20.2, 19.7, 20.5, 20.0],
        metric="cpu_pct",
    )

    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    assert res["n_spikes"] == 0
    # z_factor низкий (after ≈ baseline mean), spike_component≈0 →
    # confidence близко к 0.
    assert res["confidence"] < 0.2
    assert res["verdict"] == "unlikely"


def test_flat_baseline_penalty(db):
    """flat baseline (stddev≈0 у всех метрик) → penalty 0.3, confidence
    падает примерно в 3 раза."""
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=5)
    _make_deploy(db, svc.id, deploy_ts, status="FAILURE")

    # cpu_pct: до — все 10.0 (stddev=0), после — 100.0 (10× больше).
    _seed_health_window(
        db, svc.id, deploy_ts,
        before_values=[10.0] * 6,
        after_values=[100.0, 100.0, 100.0, 100.0],
        metric="cpu_pct",
    )

    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    # max_zscore не должен посчитаться (flat baseline для единственной метрики
    # с данными). Остальные метрики None → тоже без z.
    assert res["max_zscore"] is None
    # spike по delta_pct сработал.
    assert res["n_spikes"] >= 1
    # flat_baseline_penalty применился (0.3).
    assert res["scoring"]["flat_baseline_penalty"] == pytest.approx(0.3)
    # Без penalty получили бы confidence ≈ time_proximity × 1.2 × (0.2*0.5)
    # = 0.85 × 1.2 × 0.1 = ~0.10. С penalty 0.3 → ~0.03.
    # Главное — penalty виден и confidence снизилась.
    assert res["confidence"] < 0.15


def test_time_proximity_far_away(db):
    """Δt=110min: время сильно ослабляет вердикт, но не обнуляет его.

    Раньше τ=30 без пола давал exp(-110/30)≈0.025, и любой деплой дальше
    ~10-27 минут не мог подняться выше `weak` даже при идеальных метриках —
    вердикт вырождался в датчик близости по времени, хотя lookback заявлен 2ч.
    Теперь τ=60 + пол 0.35: metric evidence доходит до вердикта на всём
    lookback-е, но далёкий деплой всё ещё остаётся консервативным (`weak`)
    и не дотягивает до suspect/likely.
    """
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=110)
    _make_deploy(db, svc.id, deploy_ts, status="FAILURE")

    # Даже с сильным spike — далёкий deploy должен дать почти 0.
    _seed_health_window(
        db, svc.id, deploy_ts,
        before_values=[10.0, 11.0, 9.5, 10.5, 10.0, 9.8],
        after_values=[100.0, 102.0, 98.0, 101.0],
        metric="cpu_pct",
    )

    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    # Время всё ещё заметно давит: сырой exp(-110/60) ≈ 0.16.
    assert res["scoring"]["time_proximity"] < 0.25
    # Но пол не даёт метрикам обнулиться — вклад времени остаётся ощутимым.
    assert 0.35 <= res["scoring"]["time_factor"] < 0.55
    # Итог остаётся консервативным: до suspect (0.4) далёкий деплой не дотягивает.
    assert res["confidence"] < 0.4
    assert res["verdict"] == "weak"


def test_backward_compat_fields_present(db):
    """Старые callers ожидают verdict, metrics_diff с delta_pct, deploy-dict."""
    svc = _make_service(db)
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=10)
    _make_deploy(db, svc.id, deploy_ts, status="SUCCESS")
    _seed_health_window(
        db, svc.id, deploy_ts,
        before_values=[20.0, 21.0, 19.5, 20.5, 20.0, 19.8],
        after_values=[20.2, 19.7, 20.5, 20.0],
        metric="cpu_pct",
    )
    res = correlate_deploy_to_incident(db, svc.id, incident_ts)
    # Старые поля.
    assert "verdict" in res
    assert "metrics_diff" in res
    assert "cpu_pct" in res["metrics_diff"]
    assert "delta_pct" in res["metrics_diff"]["cpu_pct"]
    assert "before" in res["metrics_diff"]["cpu_pct"]
    assert "after" in res["metrics_diff"]["cpu_pct"]
    # deploy-dict со всеми ключами.
    dep = res["deploy"]
    for k in ("id", "service_id", "sha", "buildtype_id", "build_number",
              "started_at", "status", "minutes_before_incident"):
        assert k in dep
    # Новые поля.
    for k in ("confidence", "n_spikes", "max_zscore",
              "time_proximity_minutes", "scoring"):
        assert k in res


def test_failure_status_boosts_over_success(db):
    """При прочих равных FAILURE должен дать confidence выше SUCCESS на 20%."""
    incident_ts = datetime(2026, 5, 23, 12, 0, 0)
    deploy_ts = incident_ts - timedelta(minutes=10)

    # Два сервиса с одинаковой метрикой — разница только в status.
    svc_a = _make_service(db, name="svc-success", namespace="squad-1")
    svc_b = _make_service(db, name="svc-failure", namespace="squad-1")
    _make_deploy(db, svc_a.id, deploy_ts, status="SUCCESS")
    _make_deploy(db, svc_b.id, deploy_ts, status="FAILURE")

    for svc_id in (svc_a.id, svc_b.id):
        _seed_health_window(
            db, svc_id, deploy_ts,
            before_values=[20.0, 21.0, 19.5, 20.5, 20.0, 19.8],
            after_values=[50.0, 52.0, 49.0, 51.0],
            metric="cpu_pct",
        )

    res_a = correlate_deploy_to_incident(db, svc_a.id, incident_ts)
    res_b = correlate_deploy_to_incident(db, svc_b.id, incident_ts)
    # FAILURE-bonus 1.2× — confidence у svc_b должен быть выше.
    assert res_b["confidence"] > res_a["confidence"]
    ratio = res_b["confidence"] / max(res_a["confidence"], 1e-9)
    # Допускаем небольшой floating-point дрейф, но соотношение около 1.2.
    assert 1.1 <= ratio <= 1.3
