"""Тесты на KG self-health canary'и (Wave 5 retrospective).

Покрытие per-check + aggregate + dedup. SQLite in-memory как и
test_knowledge_graph.py.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        AlertEvent, AnomalyObservation,
                                        ClusterObservation, Deployment,
                                        LogObservation, Namespace,
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
    # Запись о namespace нужна проверкам, которые считают только живые
    # окружения (edges_freshness): без неё join не находит ничего.
    if not db.query(Namespace).filter_by(namespace=ns).first():
        db.add(Namespace(namespace=ns, state=NS_STATE_ACTIVE, incarnation=1,
                         first_seen_at=datetime.utcnow(),
                         last_seen_at=datetime.utcnow()))
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
    assert per_metric["mem_pct"]["missing_pct"] == 100.0
    # cpu_pct норм — 0 нулей
    assert per_metric["cpu_pct"].get("status") == "ok"


def test_materialization_zero_rate_known_zero_metric_is_ok(db):
    """http_5xx_rate/p95_latency_ms в allowlist — 100% пропусков всё равно ok.

    Значения именно NULL, а не 0. Это то, что происходит на проде: обе
    колонки не заполняются вовсе (362 677 NULL из 362 677 строк за сутки),
    потому что WO scrape config не отдаёт nginx_ingress-метрики. С нулями
    тест ничего бы не проверял: для счётчика событий ноль — законное
    значение, и он прошёл бы как ok даже без allowlist'а.
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
            http_5xx_rate=None,   # allowlisted: не собирается вовсе
            p95_latency_ms=None,  # allowlisted
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "ok"
    pm = result.detail["per_metric"]
    assert pm["http_5xx_rate"]["allowlisted"] is True
    assert pm["p95_latency_ms"]["allowlisted"] is True
    # cpu_pct — не в allowlist, но нулей мало
    assert pm["cpu_pct"]["missing_pct"] == 0.0


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


def test_event_rate_metric_full_of_zeros_is_not_a_failure(db):
    """99% нулей в restarts_rate — это здоровый кластер, а не поломка.

    Регрессия на ложное срабатывание, которое держало самопроверку в fail
    постоянно. Замер на проде за сутки 20.08.2026: у `restarts_rate` было
    358 995 нулей, 3 668 положительных значений и 14 NULL. Метрика писалась
    исправно и ловила настоящие рестарты, но правило «>90% нулей = fail»
    объявляло её несуществующей — потому что ноль в счётчике событий
    означает «событий не было», а не «значение потеряно».

    `restarts_rate` НЕ в allowlist'е: он и не должен там быть, метрика
    рабочая и по ней надо алёртить, если она реально пропадёт.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    for i in range(100):
        _mk_health_row(
            db, svc,
            ts=now - timedelta(minutes=10 * i),
            cpu_pct=15.0,
            mem_pct=30.0,
            # 99 нулей и одно положительное — пропорция как на проде
            restarts_rate=0.5 if i == 0 else 0.0,
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "ok", (
        "нули в счётчике событий снова читаются как поломка материализации"
    )
    pm = result.detail["per_metric"]["restarts_rate"]
    assert pm["criterion"] == "null_only"
    assert pm["missing_pct"] == 0.0
    assert pm["allowlisted"] is False


def test_event_rate_metric_stops_being_written_is_a_failure(db):
    """А вот если счётчик перестали писать — это fail, и он должен звучать.

    Обратная сторона предыдущего теста: смягчив критерий для счётчиков, мы
    обязаны сохранить способность увидеть настоящий отказ сбора. Отличие
    ровно одно — NULL вместо нуля.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    for i in range(100):
        _mk_health_row(
            db, svc,
            ts=now - timedelta(minutes=10 * i),
            cpu_pct=15.0,
            mem_pct=30.0,
            restarts_rate=None,   # значение не записано вовсе
        )

    result = check_materialization_zero_rate(db)
    assert result.status == "fail"
    pm = result.detail["per_metric"]["restarts_rate"]
    assert pm["status"] == "fail"
    assert pm["missing_pct"] == 100.0


def test_materialization_share_never_exceeds_one_hundred_percent(db):
    """Доля пропусков физически не может быть больше 100%.

    Регрессия на прод-замер 21.08.2026: `http_5xx_rate missing=100.5%`.
    Знаменатель считался отдельным запросом и раньше числителей, а
    metrics_sync успевал вставить строки между ними. Само число безобидное,
    но проверка, печатающая невозможную величину, перестаёт быть
    свидетельством. Теперь всё берётся одним запросом — одним снимком.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    for i in range(30):
        _mk_health_row(
            db, svc,
            ts=now - timedelta(minutes=5 * i),
            cpu_pct=0.0, mem_pct=0.0,
            restarts_rate=None, http_5xx_rate=None, p95_latency_ms=None,
        )
    db.commit()

    result = check_materialization_zero_rate(db)
    for name, info in result.detail["per_metric"].items():
        assert 0.0 <= info["missing_pct"] <= 100.0, f"{name}: {info['missing_pct']}%"
        assert info["rows"] == result.detail["total_rows"], (
            f"{name}: знаменатель разъехался с общим числом строк"
        )


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


def test_sync_lag_seq_quiet_window_is_ok(db, monkeypatch):
    """Тихое окно в логах — не поломка синка.

    Запись в kg_log_observations появляется только когда в окне были
    Error/Fatal/Warning. Замер на проде 21.08.2026: за час на shared-инстансе
    7 таких событий, но в двух 10-минутных окнах подряд — ноль. При пороге
    fail = 5×interval = 50 минут тихая ночь давала ложный fail, ровно как
    было у kg_anomaly_detection_task до перевода на heartbeat.
    """
    svc = _mk_service(db)
    now = datetime.utcnow()
    _patch_heartbeat(monkeypatch, minutes_ago=3)   # синк опросил 3 минуты назад

    # Данных нет вообще — в логах было тихо.
    db.add(ServiceHealth(service_id=svc.id, ts=now, cpu_pct=10.0, source="vm"))
    db.add(ClusterObservation(ts=now))
    db.add(AnomalyObservation(
        service_id=svc.id, ts=now, metric="cpu_pct", severity="warning",
    ))
    db.add(SignalAggregate(service_id=svc.id, window_end=now, window_hours=24))
    db.commit()

    seq = check_sync_lag(db).detail["per_task"]["kg_seq_logs_sync"]
    assert seq["source"] == "heartbeat"
    assert seq["status"] == "ok", "тишина в логах снова читается как поломка синка"


def test_sync_lag_seq_fails_when_sync_stops_running(db, monkeypatch):
    """А если синк не отчитывался — fail, и тут heartbeat ничего не прячет.

    Обратная сторона: heartbeat пишется только для прогонов без
    error-маркера, а `sync_seq_logs` возвращает error, когда не ответил ни
    один инстанс Seq. Так ловится 20.08.2026 — NetworkPolicy перекрыла
    доступ ко всем восьми инстансам, задача завершалась SUCCESS с rows=0, и
    отсутствие данных 12,8 часа никого не тревожило.
    """
    _mk_service(db)
    _patch_heartbeat(monkeypatch, minutes_ago=120)   # молчит два часа
    seq = check_sync_lag(db).detail["per_task"]["kg_seq_logs_sync"]
    assert seq["status"] == "fail"
    assert seq["lag_minutes"] >= 119.0


def _patch_heartbeat(monkeypatch, minutes_ago=None):
    """Подменить чтение redis-heartbeat'а в self_health.

    minutes_ago=None → ключа нет (redis перезапускался / первый запуск).
    """
    import app.knowledge_graph.self_health as sh

    ts = (
        datetime.utcnow() - timedelta(minutes=minutes_ago)
        if minutes_ago is not None else None
    )
    monkeypatch.setattr(sh, "_last_heartbeat", lambda task: ts)
    return ts


def test_sync_lag_anomaly_ok_on_quiet_period(db, monkeypatch):
    """Ложный fail (review 2026-08): задача ходит каждые 10 мин, но за час не
    нашла ни одной аномалии — это НОРМА (noise-floor + volume guard), а не
    поломка. Раньше max(AnomalyObservation.ts) отсутствовал → fail.
    """
    _patch_heartbeat(monkeypatch, minutes_ago=3)
    result = check_sync_lag(db)
    anomaly = result.detail["per_task"]["kg_anomaly_detection_task"]
    assert anomaly["status"] == "ok"
    assert anomaly["source"] == "heartbeat"
    # Результата нет вообще — и это не влияет на статус.
    assert anomaly["last_result_ts"] is None


def test_sync_lag_anomaly_ignores_stale_result_while_task_runs(db, monkeypatch):
    """Последняя аномалия 6 часов назад, но задача прогонялась 3 минуты назад →
    ok. Наличие РЕЗУЛЬТАТА не является признаком живости детектора."""
    svc = _mk_service(db)
    db.add(AnomalyObservation(
        service_id=svc.id, ts=datetime.utcnow() - timedelta(hours=6),
        metric="cpu_pct", severity="warning",
    ))
    db.commit()
    _patch_heartbeat(monkeypatch, minutes_ago=3)
    result = check_sync_lag(db)
    anomaly = result.detail["per_task"]["kg_anomaly_detection_task"]
    assert anomaly["status"] == "ok"
    assert anomaly["last_result_ts"] is not None


def test_sync_lag_anomaly_fails_when_task_stopped(db, monkeypatch):
    """А вот когда сама задача не прогонялась 90 мин (>5× интервала 10 мин) —
    это настоящий fail, и его heartbeat как раз видит."""
    _patch_heartbeat(monkeypatch, minutes_ago=90)
    result = check_sync_lag(db)
    anomaly = result.detail["per_task"]["kg_anomaly_detection_task"]
    assert anomaly["status"] == "fail"
    assert result.status == "fail"


def test_sync_lag_anomaly_warns_without_heartbeat(db, monkeypatch):
    """Нет ключа в redis (перезапуск redis / свежий инстанс) → warn, не fail:
    «неизвестно» ≠ «задача не ходит»."""
    _patch_heartbeat(monkeypatch, minutes_ago=None)
    result = check_sync_lag(db)
    anomaly = result.detail["per_task"]["kg_anomaly_detection_task"]
    assert anomaly["status"] == "warn"
    assert anomaly["lag_minutes"] is None
    assert "heartbeat" in anomaly["reason"]


def test_quiet_anomalies_do_not_produce_conflicting_verdicts(db, monkeypatch):
    """Корень ложного алерта: тихие 24h check_anomaly_signal_health считает
    warn, а sync_lag считал fail — fingerprint-dedup (по набору fail-ов) потом
    надолго закреплял этот fail в Discord. Теперь противоречия нет."""
    _patch_heartbeat(monkeypatch, minutes_ago=5)
    lag = check_sync_lag(db).detail["per_task"]["kg_anomaly_detection_task"]
    signal = check_anomaly_signal_health(db)
    assert lag["status"] == "ok"
    assert signal.status == "warn"          # «детектор молчит» — мягкий сигнал
    assert "anomaly_signal_health" not in fingerprint([signal])


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


def test_anomaly_signal_health_many_observations_alone_is_not_a_problem(db):
    """Большое число наблюдений само по себе не признак болезни.

    Абсолютный порог «>500 = шумит» стоял с эпохи, когда сервисов было в разы
    меньше. Замер 21.08.2026: 11 325 сервисов в графе, аномалии у 859 (7,6%),
    наблюдений 21 303 — проверка держалась в warn постоянно и ничего этим не
    сообщала. Здесь 600 наблюдений размазаны по 60 сервисам, каждый аномален
    в считанные часы: это работающий детектор, а не захлебнувшийся.
    """
    now = datetime.utcnow()
    for i in range(60):
        svc = _mk_service(db, name=f"svc-{i}")
        for j in range(10):
            db.add(AnomalyObservation(
                service_id=svc.id,
                ts=now - timedelta(hours=j % 3, minutes=j * 7),
                metric=f"metric_{j % 5}",
                severity="warning",
            ))
    db.commit()
    result = check_anomaly_signal_health(db)
    assert result.status == "ok", result.detail
    assert result.detail["count_24h"] == 600
    assert result.detail["always_on_services"] == 0


def test_anomaly_signal_health_warns_on_permanently_anomalous_services(db):
    """А вот сервис, аномальный круглые сутки, — это сменившаяся норма.

    У такого нет «отклонения»: baseline отстал от реальности. Замер
    21.08.2026: 133 сервиса из 859 аномальны больше двадцати часов из
    двадцати четырёх, и главная причина известна — после пересоздания стенда
    в baseline-окно попадают замеры прежнего (262 657 точек, 797 сервисов).
    """
    now = datetime.utcnow()
    # 4 сервиса аномальны почти круглосуточно, 6 — эпизодически:
    # доля постоянных 40% > порога 25%.
    for i in range(4):
        svc = _mk_service(db, name=f"always-{i}")
        for h in range(24):
            db.add(AnomalyObservation(
                service_id=svc.id, ts=now - timedelta(hours=h),
                metric="cpu_pct", severity="critical",
            ))
    for i in range(6):
        svc = _mk_service(db, name=f"rare-{i}")
        db.add(AnomalyObservation(
            service_id=svc.id, ts=now - timedelta(minutes=5),
            metric="cpu_pct", severity="warning",
        ))
    db.commit()

    result = check_anomaly_signal_health(db)
    assert result.status == "warn"
    assert result.detail["always_on_services"] == 4
    assert result.detail["services_with_anomalies"] == 10
    assert result.detail["always_on_share"] == 0.4


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


def test_edges_freshness_ignores_edges_of_removed_environments(db):
    """Рёбра снесённого окружения устаревают законно — это retention.

    Замер 22.08.2026: из 7583 устаревших рёбер 6811 (90%) принадлежали
    missing-namespace, и доля 30,8% при пороге 30% дала warn на исправном
    контуре. Среди живых окружений устаревших было 772 из 17 408 — 4,4%.
    """
    live = _mk_service(db, "live-svc", "prod-shared")
    dead = _mk_service(db, "dead-svc", "squad-20-shared")
    db.query(Namespace).filter_by(namespace="squad-20-shared").update(
        {"state": NS_STATE_MISSING}
    )
    now = datetime.utcnow()
    # живое окружение: одно ребро, свежее
    db.add(ServiceEdge(src_id=live.id, dst_id=dead.id, kind="calls",
                       last_seen_at=now))
    # снесённое: двадцать устаревших — они не должны влиять на статус
    for i in range(20):
        db.add(ServiceEdge(src_id=dead.id, dst_id=live.id, kind=f"stale-{i}",
                           last_seen_at=now - timedelta(days=5)))
    db.commit()

    result = check_edges_freshness(db)
    assert result.status == "ok", result.detail
    assert result.detail["total"] == 1
    assert result.detail["scope"] == "active_namespaces_only"


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
