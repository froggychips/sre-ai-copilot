"""Тесты на KG self-health canary'и (Wave 5 retrospective).

Покрытие per-check + aggregate + dedup. SQLite in-memory как и
test_knowledge_graph.py.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        ClusterObservation, Deployment,
                                        LogObservation,
                                        PodEvent, Service, ServiceEdge,
                                        ServiceHealth, SignalAggregate)
from app.knowledge_graph.self_health import (aggregate_status,
                                             check_alerts_resolve_freshness,
                                             check_anomaly_signal_health,
                                             check_deploy_stream_ingestion,
                                             check_edges_freshness,
                                             check_materialization_zero_rate,
                                             check_pod_events_link_rate,
                                             check_sync_lag, fingerprint,
                                             run_self_health_checks)


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


def _mk_service(db, name="svc-a", ns="squad-1") -> Service:
    s = Service(name=name, namespace=ns, team_owner="squad-1")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _mk_health_row(
    db,
    svc: Service,
    ts: datetime,
    cpu_pct=10.0,
    mem_pct=20.0,
    restarts_rate=0.0,
    http_5xx_rate=0.0,
    p95_latency_ms=0.0,
) -> ServiceHealth:
    row = ServiceHealth(
        service_id=svc.id,
        ts=ts,
        cpu_pct=cpu_pct,
        mem_pct=mem_pct,
        restarts_rate=restarts_rate,
        http_5xx_rate=http_5xx_rate,
        p95_latency_ms=p95_latency_ms,
        source="vm",
    )
    db.add(row)
    db.commit()
    return row


# ── check_materialization_zero_rate ───────────────────────────────────────


def test_materialization_zero_rate_wave5_reproducer_mem_all_zero(db):
    """ОРИГИНАЛЬНЫЙ wave 5 bug: mem_pct=0 на всех записях → fail."""
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 100 ровных тиков за 24h, все mem_pct=0
    for i in range(100):
        _mk_health_row(
            db, svc,
            ts=now - timedelta(hours=24) + timedelta(minutes=10 * i),
            cpu_pct=12.5,
            mem_pct=0.0,
            restarts_rate=0.1,
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "fail"
    per_metric = result.detail["per_metric"]
    assert per_metric["mem_pct"]["status"] == "fail"
    assert per_metric["mem_pct"]["zero_or_null_pct"] == 100.0
    # cpu_pct норм — 0 нулей
    assert per_metric["cpu_pct"].get("status") == "ok"


def test_materialization_zero_rate_known_zero_metric_is_ok(db):
    """http_5xx_rate/p95_latency_ms в allowlist — 100% нулей всё равно ok.

    Остальные метрики ставим в нормальные значения, чтобы изолировать
    эффект allowlist'а.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    for i in range(50):
        _mk_health_row(
            db, svc,
            ts=now - timedelta(minutes=10 * i),
            cpu_pct=15.0,
            mem_pct=30.0,
            restarts_rate=0.5,
            http_5xx_rate=0.0,   # allowlisted, не алёртим
            p95_latency_ms=0.0,  # allowlisted
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "ok"
    pm = result.detail["per_metric"]
    assert pm["http_5xx_rate"]["allowlisted"] is True
    assert pm["p95_latency_ms"]["allowlisted"] is True
    # cpu_pct — не в allowlist, но нулей мало
    assert pm["cpu_pct"]["zero_or_null_pct"] == 0.0


def test_materialization_zero_rate_warn_threshold(db):
    """71% < zero rate <= 90% → warn.

    Все «не-cpu_pct» метрики держим non-zero, чтобы изолировать сигнал
    по cpu_pct.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 8/10 = 80% нулей по cpu_pct
    for i in range(10):
        cpu = 0.0 if i < 8 else 25.0
        _mk_health_row(
            db, svc,
            ts=now - timedelta(minutes=10 * i),
            cpu_pct=cpu,
            mem_pct=30.0,
            restarts_rate=0.5,
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "warn"
    assert result.detail["per_metric"]["cpu_pct"]["status"] == "warn"


def test_materialization_zero_rate_no_data_returns_ok(db):
    """Нет записей — это вотчина sync_lag, тут не плодим дубли."""
    result = check_materialization_zero_rate(db)
    assert result.status == "ok"
    assert result.detail["total_rows"] == 0


# ── check_sync_lag ────────────────────────────────────────────────────────


def test_sync_lag_fails_when_no_data_at_all(db):
    """Полностью пустая БД — каждая таска fail (no last_ts)."""
    result = check_sync_lag(db)
    assert result.status == "fail"
    # все ожидаемые таски в результате
    assert "kg_metrics_sync" in result.detail["per_task"]
    assert result.detail["per_task"]["kg_metrics_sync"]["status"] == "fail"


def test_sync_lag_seq_30min_gap_fails(db):
    """Seq не пишет 30 мин при ожидаемом 10 мин → lag 3× → warn,
    но в 30 мин ещё не >5× (=50 мин), значит warn. Проверим оба порога."""
    svc = _mk_service(db)
    now = datetime.utcnow()

    # Seq последний раз писал 30 мин назад — 3× interval (=10 мин)
    db.add(LogObservation(
        service_id=svc.id,
        ts=now - timedelta(minutes=30),
        level="Error",
        count=1,
        source="prod",
    ))
    # Заполним остальные таски свежими данными, чтобы изолировать seq
    db.add(ServiceHealth(service_id=svc.id, ts=now, cpu_pct=10.0, source="vm"))
    db.add(ClusterObservation(ts=now))
    db.add(AnomalyObservation(
        service_id=svc.id, ts=now, metric="cpu_pct", severity="warning",
    ))
    db.add(SignalAggregate(service_id=svc.id, window_end=now, window_hours=24))
    db.commit()
    # Service.updated_at — onupdate=datetime.utcnow(), при первом commit заполнен
    # default'ом, который тоже now.

    result = check_sync_lag(db)
    seq = result.detail["per_task"]["kg_seq_logs_sync"]
    assert seq["status"] == "warn"
    assert seq["lag_minutes"] >= 29.0  # допуск на округление

    # Теперь поставим Seq lag = 60 мин (6× → fail)
    db.query(LogObservation).delete()
    db.add(LogObservation(
        service_id=svc.id,
        ts=now - timedelta(minutes=60),
        level="Error",
        count=1,
        source="prod",
    ))
    db.commit()
    result2 = check_sync_lag(db)
    assert result2.detail["per_task"]["kg_seq_logs_sync"]["status"] == "fail"
    assert result2.status == "fail"


def test_sync_lag_ok_when_fresh(db):
    """Все данные свежие — ok."""
    svc = _mk_service(db)
    now = datetime.utcnow()
    db.add(ServiceHealth(service_id=svc.id, ts=now, cpu_pct=10.0, source="vm"))
    db.add(ClusterObservation(ts=now))
    db.add(LogObservation(service_id=svc.id, ts=now, level="Error", count=1, source="prod"))
    db.add(AnomalyObservation(
        service_id=svc.id, ts=now, metric="cpu_pct", severity="warning",
    ))
    db.add(SignalAggregate(service_id=svc.id, window_end=now, window_hours=24))
    db.commit()

    result = check_sync_lag(db)
    # Service.updated_at должен быть свежим из default
    assert result.detail["per_task"]["kg_topology_sync"]["status"] == "ok"
    assert result.detail["per_task"]["kg_metrics_sync"]["status"] == "ok"


# ── check_anomaly_signal_health ───────────────────────────────────────────


def test_anomaly_signal_health_zero_obs_warns(db):
    result = check_anomaly_signal_health(db)
    assert result.status == "warn"
    assert result.detail["count_24h"] == 0


def test_anomaly_signal_health_ok_in_range(db):
    svc = _mk_service(db)
    now = datetime.utcnow()
    for i in range(5):
        db.add(AnomalyObservation(
            service_id=svc.id, ts=now - timedelta(minutes=10 * i),
            metric="cpu_pct", severity="warning",
        ))
    db.commit()
    result = check_anomaly_signal_health(db)
    assert result.status == "ok"
    assert result.detail["count_24h"] == 5


def test_anomaly_signal_health_overload_warns(db):
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 600 observations за 24h → warn (overload)
    for i in range(600):
        db.add(AnomalyObservation(
            service_id=svc.id,
            ts=now - timedelta(minutes=i % 1000),
            metric=f"metric_{i % 5}",
            severity="warning",
        ))
    db.commit()
    result = check_anomaly_signal_health(db)
    assert result.status == "warn"
    assert result.detail["count_24h"] > 500


# ── check_alerts_resolve_freshness ────────────────────────────────────────


def test_alerts_resolve_freshness_warn_on_many_stale_open(db):
    """Wave 1 Track B: >20 alert'ов >7d unresolved → warn."""
    svc = _mk_service(db)
    old = datetime.utcnow() - timedelta(days=10)
    for i in range(25):
        db.add(AlertEvent(
            service_id=svc.id,
            alertname=f"X{i}",
            fingerprint=f"fp-{i}",
            fired_at=old,
            resolved_at=None,
        ))
    db.commit()
    result = check_alerts_resolve_freshness(db)
    assert result.status == "warn"
    assert result.detail["stale_open_alerts"] == 25


def test_alerts_resolve_freshness_ok_when_resolved(db):
    svc = _mk_service(db)
    old = datetime.utcnow() - timedelta(days=10)
    for i in range(25):
        db.add(AlertEvent(
            service_id=svc.id,
            alertname=f"X{i}",
            fingerprint=f"fp-{i}",
            fired_at=old,
            resolved_at=old + timedelta(hours=1),
        ))
    db.commit()
    result = check_alerts_resolve_freshness(db)
    assert result.status == "ok"


# ── check_pod_events_link_rate ────────────────────────────────────────────


def test_pod_events_link_rate_fail_when_below_50_pct(db):
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 10 событий, 3 linked → 30%
    for i in range(10):
        db.add(PodEvent(
            service_id=svc.id if i < 3 else None,
            namespace="squad-1",
            pod_name=f"pod-{i}",
            reason="OOMKilled",
            event_uid=f"uid-{i}",
            first_seen=now - timedelta(hours=1),
        ))
    db.commit()
    result = check_pod_events_link_rate(db)
    assert result.status == "fail"
    assert result.detail["linked_pct"] == 30.0


def test_pod_events_link_rate_warn_between_50_and_80(db):
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 10 событий, 7 linked → 70% → warn
    for i in range(10):
        db.add(PodEvent(
            service_id=svc.id if i < 7 else None,
            namespace="squad-1",
            pod_name=f"pod-{i}",
            reason="OOMKilled",
            event_uid=f"uid-{i}",
            first_seen=now - timedelta(hours=1),
        ))
    db.commit()
    result = check_pod_events_link_rate(db)
    assert result.status == "warn"


def test_pod_events_link_rate_ok_when_above_80(db):
    svc = _mk_service(db)
    now = datetime.utcnow()
    # 10 событий, 9 linked → 90% → ok
    for i in range(10):
        db.add(PodEvent(
            service_id=svc.id if i < 9 else None,
            namespace="squad-1",
            pod_name=f"pod-{i}",
            reason="OOMKilled",
            event_uid=f"uid-{i}",
            first_seen=now - timedelta(hours=1),
        ))
    db.commit()
    result = check_pod_events_link_rate(db)
    assert result.status == "ok"


def test_pod_events_link_rate_no_events_returns_ok(db):
    result = check_pod_events_link_rate(db)
    assert result.status == "ok"
    assert result.detail["total"] == 0


# ── check_edges_freshness ─────────────────────────────────────────────────


def test_edges_freshness_warn_when_30_pct_stale(db):
    a = _mk_service(db, "a")
    b = _mk_service(db, "b")
    c = _mk_service(db, "c")
    now = datetime.utcnow()
    # 10 рёбер: 4 свежих, 6 stale → 60% stale → warn
    fresh_pairs = [(a, b), (b, c), (a, c), (c, a)]
    for src, dst in fresh_pairs:
        db.add(ServiceEdge(
            src_id=src.id, dst_id=dst.id, kind=f"k-{src.id}-{dst.id}",
            last_seen_at=now,
        ))
    # 6 рёбер либо с NULL, либо устаревших
    for i in range(6):
        db.add(ServiceEdge(
            src_id=a.id, dst_id=b.id, kind=f"stale-{i}",
            last_seen_at=None if i % 2 == 0 else now - timedelta(days=3),
        ))
    db.commit()
    result = check_edges_freshness(db)
    assert result.status == "warn"
    assert result.detail["stale_pct"] >= 50.0


def test_edges_freshness_ok_when_all_fresh(db):
    a = _mk_service(db, "a")
    b = _mk_service(db, "b")
    now = datetime.utcnow()
    db.add(ServiceEdge(src_id=a.id, dst_id=b.id, kind="calls", last_seen_at=now))
    db.commit()
    result = check_edges_freshness(db)
    assert result.status == "ok"


# ── aggregate / orchestrator ──────────────────────────────────────────────


def test_run_self_health_checks_returns_all_results(db):
    """Orchestrator вызывает все 6 проверок и возвращает 6 результатов."""
    results = run_self_health_checks(db)
    names = {r.name for r in results}
    expected = {
        "materialization_zero_rate",
        "sync_lag",
        "anomaly_signal_health",
        "alerts_resolve_freshness",
        "pod_events_link_rate",
        "edges_freshness",
    }
    assert expected.issubset(names)


def test_aggregate_status_fail_dominates():
    from app.knowledge_graph.self_health import CheckResult
    r = [
        CheckResult("a", "ok", {}),
        CheckResult("b", "warn", {}),
        CheckResult("c", "fail", {}),
    ]
    assert aggregate_status(r) == "fail"


def test_aggregate_status_warn_when_no_fail():
    from app.knowledge_graph.self_health import CheckResult
    r = [
        CheckResult("a", "ok", {}),
        CheckResult("b", "warn", {}),
    ]
    assert aggregate_status(r) == "warn"


def test_aggregate_status_ok_when_all_ok():
    from app.knowledge_graph.self_health import CheckResult
    r = [CheckResult("a", "ok", {}), CheckResult("b", "ok", {})]
    assert aggregate_status(r) == "ok"


def test_fingerprint_only_includes_failed_checks():
    from app.knowledge_graph.self_health import CheckResult
    r = [
        CheckResult("a", "ok", {}),
        CheckResult("b", "warn", {}),
        CheckResult("c", "fail", {}),
        CheckResult("d", "fail", {}),
    ]
    fp = fingerprint(r)
    # sorted alphabetically
    assert fp == "c,d"


def test_fingerprint_stable_across_order():
    from app.knowledge_graph.self_health import CheckResult
    r1 = [CheckResult("z", "fail", {}), CheckResult("a", "fail", {})]
    r2 = [CheckResult("a", "fail", {}), CheckResult("z", "fail", {})]
    assert fingerprint(r1) == fingerprint(r2)


# ── check_deploy_stream_ingestion ───────────────────────────────────────────

def _patch_tc(monkeypatch, builds):
    """Подменить recent_deploys (async) + branch_for_namespace в teamcity_service
    (check импортирует их локально оттуда)."""
    import app.services.teamcity_service as tc

    async def _fake_recent(**_kw):
        return builds

    monkeypatch.setattr(tc, "recent_deploys", _fake_recent)
    monkeypatch.setattr(
        tc, "branch_for_namespace",
        lambda ns: "preprod" if ns == "preprod-shared" else None,
    )


def test_deploy_stream_ingestion_ok_when_all_present(db, monkeypatch):
    svc = _mk_service(db, "auth", "preprod-shared")
    db.add(Deployment(service_id=svc.id, buildtype_id="BT", build_number="100",
                      started_at=datetime.utcnow()))
    db.commit()
    _patch_tc(monkeypatch, [{"buildtype_id": "BT", "number": "100", "branch": "preprod"}])
    r = check_deploy_stream_ingestion(db)
    assert r.status == "ok"
    assert r.detail["present_in_kg"] == 1


def test_deploy_stream_ingestion_fail_when_all_missing(db, monkeypatch):
    _mk_service(db, "auth", "preprod-shared")
    db.commit()
    # TC отдаёт 2 deploy-ветко-билда, в KG их нет → ingestion сломан
    _patch_tc(monkeypatch, [
        {"buildtype_id": "BT", "number": "200", "branch": "<default>"},  # норм → preprod
        {"buildtype_id": "BT", "number": "201", "branch": "preprod"},
    ])
    r = check_deploy_stream_ingestion(db)
    assert r.status == "fail"
    assert r.detail["missing"] == 2


def test_deploy_stream_ingestion_ok_when_nothing_to_ingest(db, monkeypatch):
    _mk_service(db, "auth", "preprod-shared")
    db.commit()
    # ветки билдов не маппятся ни на один KG-ns → нечего ингестить (тихо)
    _patch_tc(monkeypatch, [{"buildtype_id": "BT", "number": "1", "branch": "feature/x"}])
    r = check_deploy_stream_ingestion(db)
    assert r.status == "ok"


def test_deploy_stream_ingestion_prod_default_not_remapped(db, monkeypatch):
    """'<default>' у prod-конфига НЕ подменяется на preprod (как и в task)."""
    _mk_service(db, "auth", "preprod-shared")
    db.commit()
    _patch_tc(monkeypatch, [
        {"buildtype_id": "Wo_..._Prod_BuildAndDeploy", "number": "9", "branch": "<default>"},
    ])
    r = check_deploy_stream_ingestion(db)
    # prod '<default>' → ветка остаётся '<default>', на preprod-shared не маппится → нечего ингестить
    assert r.status == "ok"
