"""health_score на ХРОНИЧЕСКИХ сигналах + freshness агрегатов (review 2026-08).

Три находки, которые тут закрыты:

1. Открытые алерты фильтровались `fired_at >= now-24h`, а record_alert_event
   для ongoing-алерта сохраняет ИСХОДНЫЙ fired_at. Медиана TTR у
   KubeDeploymentReplicasMismatch — 29h, p90 = 83h: critical, горящий вторые
   сутки, полностью выпадал из alert-penalty → score сервиса в разгар
   многодневной аварии возвращался к ~1.0, и top_unhealthy его не показывал.
2. chronic_pod_events фильтровались по first_seen. k8s агрегирует повторы в
   ОДНО событие с растущим count, поэтому у живого BackOff с count=11789
   first_seen недельной давности — терялся ровно самый хронический кейс.
3. Freshness агрегатов: `kg-signal-aggregates-compute` пишет hourly в :23 с
   window_end=floor(hour), а `kg-health-recompute` бежит */20. Допуск в 1 час
   выкидывал агрегат в прогонах :00/:20 и пропускал в :40 → deploy_failure/
   slo_burn-штраф флапал каждые 20 минут.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.health_score import (compute_health_for_service,
                                              top_unhealthy)
from app.knowledge_graph.schema import (AlertEvent, PodEvent, Service,
                                        SignalAggregate)

# «Сейчас» для всех тестов — фиксированное, чтобы окна считались стабильно.
_NOW = datetime(2026, 8, 10, 13, 20, 0)


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


def _svc(db, name="bot-service", ns="prod-kingdom5"):
    s = Service(name=name, namespace=ns, synthetic=False, team_owner="squad-1")
    db.add(s)
    db.flush()
    return s


def _alert(db, svc, *, hours_ago, severity="critical", resolved_hours_ago=None,
           alertname="KubeDeploymentReplicasMismatch", fp=None):
    a = AlertEvent(
        service_id=svc.id,
        alertname=alertname,
        severity=severity,
        fingerprint=fp or f"fp-{alertname}-{hours_ago}-{severity}",
        fired_at=_NOW - timedelta(hours=hours_ago),
        resolved_at=(
            _NOW - timedelta(hours=resolved_hours_ago)
            if resolved_hours_ago is not None else None
        ),
    )
    db.add(a)
    db.flush()
    return a


def _pod_event(db, svc, *, count, first_seen_days_ago, last_seen_days_ago,
               reason="BackOff", uid=None):
    e = PodEvent(
        service_id=svc.id,
        namespace=svc.namespace,
        pod_name="bot-service-7d9f-x2k",
        reason=reason,
        event_uid=uid or f"uid-{reason}-{count}-{first_seen_days_ago}",
        first_seen=_NOW - timedelta(days=first_seen_days_ago),
        last_seen=(
            _NOW - timedelta(days=last_seen_days_ago)
            if last_seen_days_ago is not None else None
        ),
        count=count,
    )
    db.add(e)
    db.flush()
    return e


# ── 1. Многодневный незарезолвленный алерт ────────────────────────────────


def test_multiday_open_critical_still_penalizes(db):
    """Critical горит 83 часа (p90 TTR) и не resolved → штраф 0.40, как и
    в первый час. Раньше окно 24h делало score равным 1.0."""
    svc = _svc(db)
    _alert(db, svc, hours_ago=83)
    db.commit()

    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["open_critical"] == 1
    assert score == pytest.approx(0.60)
    # Recurrence-окно осталось 24h: тот же алерт не должен считаться ещё и
    # «повторяющимся» — двойного штрафа быть не должно.
    assert signals["recurrence_24h"] == 0


def test_multiday_open_warning_penalizes(db):
    svc = _svc(db)
    _alert(db, svc, hours_ago=52, severity="warning")
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["open_warning"] == 1
    assert score == pytest.approx(0.85)


def test_resolved_old_alert_does_not_penalize(db):
    """Смысл фильтра остался прежним: штрафуем «всё ещё открыт», а не «был»."""
    svc = _svc(db)
    _alert(db, svc, hours_ago=40, resolved_hours_ago=2)
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["open_critical"] == 0
    assert score == 1.0


def test_phantom_open_alert_older_than_30d_ignored(db):
    """Фантом сломанного resolve-пути (открыт 45 дней) не пришпиливает сервис
    к нулю навсегда — это вотчина check_alerts_resolve_freshness."""
    svc = _svc(db)
    _alert(db, svc, hours_ago=45 * 24)
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["open_critical"] == 0
    assert score == 1.0


def test_multiday_incident_surfaces_in_top_unhealthy(db):
    """Сквозной смысл находки: сервис с многодневным critical должен быть
    виден в top_unhealthy (дайджест «🩺 Top unhealthy»), а не в перфект-зоне."""
    hot = _svc(db, name="bot-service")
    calm = _svc(db, name="auth", ns="prod-shared")
    _alert(db, hot, hours_ago=70)
    db.commit()

    for s in (hot, calm):
        score, _ = compute_health_for_service(db, s, now=_NOW)
        s.health_score = score
        s.health_computed_at = _NOW
    db.commit()

    rows = top_unhealthy(db, limit=5)
    assert rows[0]["name"] == "bot-service"
    assert rows[0]["health_score"] == pytest.approx(0.60)


# ── 2. chronic_pod_events по last_seen ────────────────────────────────────


def test_chronic_pod_event_counted_by_last_seen(db):
    """BackOff count=11789: first_seen 20 дней назад (вне окна 7д), last_seen
    час назад → событие живое и должно штрафовать."""
    svc = _svc(db)
    _pod_event(db, svc, count=11789, first_seen_days_ago=20,
               last_seen_days_ago=0.04)
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["chronic_pod_events"] == 1
    assert score == pytest.approx(0.65)


def test_chronic_pod_event_stale_last_seen_not_counted(db):
    """Событие затихло 10 дней назад — вне окна 7д, штрафа нет."""
    svc = _svc(db)
    _pod_event(db, svc, count=11789, first_seen_days_ago=20,
               last_seen_days_ago=10)
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["chronic_pod_events"] == 0
    assert score == 1.0


def test_chronic_pod_event_null_last_seen_falls_back_to_first_seen(db):
    """last_seen nullable (строки без dedup-апдейта) → fallback на first_seen."""
    svc = _svc(db)
    _pod_event(db, svc, count=2000, first_seen_days_ago=1,
               last_seen_days_ago=None, uid="null-ls-fresh")
    _pod_event(db, svc, count=2000, first_seen_days_ago=9,
               last_seen_days_ago=None, uid="null-ls-stale")
    db.commit()
    _, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["chronic_pod_events"] == 1


def test_low_count_pod_event_not_chronic(db):
    """Порог count > 1000 не тронут: единичный BackOff — не хроника."""
    svc = _svc(db)
    _pod_event(db, svc, count=12, first_seen_days_ago=0, last_seen_days_ago=0)
    db.commit()
    score, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["chronic_pod_events"] == 0
    assert score == 1.0


# ── 3. Freshness агрегатов на границах расписания ─────────────────────────

# Агрегат, записанный в 12:23 с window_end = floor(hour) = 12:00.
_AGG_WINDOW_END = datetime(2026, 8, 10, 12, 0, 0)


def _agg(db, svc, *, window_end=_AGG_WINDOW_END, slo_burn_pct=30.0):
    a = SignalAggregate(
        service_id=svc.id,
        window_end=window_end,
        window_hours=24,
        deploy_count=0,
        deploy_failure_pct=0.0,
        alert_open_count=1,
        pod_event_count=0,
        slo_burn_pct=slo_burn_pct,
    )
    db.add(a)
    db.flush()
    return a


@pytest.mark.parametrize("recompute_at", [
    datetime(2026, 8, 10, 12, 40, 0),  # +40 мин от window_end
    datetime(2026, 8, 10, 13, 0, 0),   # +60 мин — граница старого допуска
    datetime(2026, 8, 10, 13, 20, 0),  # +80 мин — тут агрегат «терялся»
])
def test_signal_aggregate_penalty_stable_across_recompute_ticks(db, recompute_at):
    """Один и тот же агрегат должен штрафовать во ВСЕХ прогонах */20 до
    следующей hourly-записи (она случится в 13:23) — иначе score флапает
    каждые 20 минут и дайджест/fragile-top видят лотерею."""
    svc = _svc(db)
    _agg(db, svc)
    db.commit()

    score, signals = compute_health_for_service(db, svc, now=recompute_at)
    # penalty = (30 - 10) * 0.01 = 0.20
    assert signals["slo_burn_pct"] == 30.0
    assert score == pytest.approx(0.80)


def test_signal_aggregate_dropped_when_hourly_writer_died(db):
    """Допуск не превращается в «вечно свежий»: если hourly-таск не писал
    несколько часов, компонент по-прежнему скипается (graceful degradation)."""
    svc = _svc(db)
    _agg(db, svc)
    db.commit()
    score, signals = compute_health_for_service(
        db, svc, now=_AGG_WINDOW_END + timedelta(hours=5),
    )
    assert signals["slo_burn_pct"] is None
    assert signals["deploy_failure_pct"] is None
    assert score == 1.0


def test_signal_aggregate_picks_newest_window(db):
    """При нескольких окнах берётся самое свежее (порядок window_end desc)."""
    svc = _svc(db)
    _agg(db, svc, window_end=_AGG_WINDOW_END - timedelta(hours=1),
         slo_burn_pct=90.0)
    _agg(db, svc, window_end=_AGG_WINDOW_END, slo_burn_pct=30.0)
    db.commit()
    _, signals = compute_health_for_service(db, svc, now=_NOW)
    assert signals["slo_burn_pct"] == 30.0
