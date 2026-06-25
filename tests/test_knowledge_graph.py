"""Тесты на minimal knowledge graph.

Используем in-memory SQLite — таблицы поднимаются из Base.metadata.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
# Импортируем schema, чтобы ORM-классы зарегистрировались в Base.metadata
from app.knowledge_graph.schema import (ServiceEdge)  # noqa: F401
from app.knowledge_graph.populator import (record_alert_event,
                                           record_deployment, record_pod_event,
                                           upsert_edge, upsert_service)
from app.knowledge_graph.queries import (incidents_on, log_error_rate_for,
                                         nearby_alerts, recent_deploys_for,
                                         upstream_of)
from app.knowledge_graph.schema import LogObservation


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


# ---------- populator -----------------------------------------------------

def test_upsert_service_creates_then_updates(db):
    s1 = upsert_service(db, "squad-1", "town-service", team_owner="gd")
    assert s1.id is not None
    s2 = upsert_service(db, "squad-1", "town-service", team_owner="gd-new")
    assert s1.id == s2.id  # тот же ID, не дубль
    assert s2.team_owner == "gd-new"


def test_upsert_edge_idempotent(db):
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    e1 = upsert_edge(db, a, b, kind="calls", weight=5)
    e2 = upsert_edge(db, a, b, kind="calls", weight=10)
    assert e1.id == e2.id
    assert e2.weight == 10


def test_upsert_edge_weight_never_lowered(db):
    """KG H5: weight монотонно растёт — env-sync (weight=1) не должен затирать
    «жирность», проставленную runtime/traffic-источником."""
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    # Runtime-источник проставил жирное ребро.
    upsert_edge(db, a, b, kind="calls", weight=42)
    # Последующий env-sync приходит с дефолтным weight=1.
    e = upsert_edge(db, a, b, kind="calls", weight=1)
    assert e.weight == 42  # НЕ понижен до 1
    # Более жирное наблюдение всё ещё поднимает.
    e2 = upsert_edge(db, a, b, kind="calls", weight=99)
    assert e2.weight == 99


def test_record_alert_idempotent_by_fingerprint(db):
    svc = upsert_service(db, "squad-1", "x")
    fired = datetime(2026, 5, 12, 10, 0)
    a1 = record_alert_event(
        db, svc, "HighLatency", "warning", "fp-1", fired
    )
    a2 = record_alert_event(
        db, svc, "HighLatency", "critical", "fp-1", fired
    )
    assert a1.id == a2.id
    assert a2.severity == "critical"


def test_record_pod_event_backfills_service_id(db):
    """H3: первый sync пришёл без сервиса (service_id=NULL),
    повторный по тому же event_uid принёс резолвящийся сервис →
    атрибуция до-проставляется (orphan-событие перестаёт быть orphan)."""
    first_seen = datetime(2026, 5, 12, 10, 0)
    e1 = record_pod_event(
        db, None, "squad-1", "town-grainhost-0", "OOMKilled",
        event_uid="uid-1", first_seen=first_seen, count=1,
    )
    assert e1.service_id is None  # сервиса ещё не было в KG

    svc = upsert_service(db, "squad-1", "town-grainhost")
    e2 = record_pod_event(
        db, svc, "squad-1", "town-grainhost-0", "OOMKilled",
        event_uid="uid-1", first_seen=first_seen, count=3,
    )
    assert e2.id == e1.id          # то же событие (идемпотентно по uid)
    assert e2.service_id == svc.id  # service_id до-проставлен
    assert e2.count == 3           # обычное обновление count тоже работает


def test_record_pod_event_does_not_overwrite_service_id(db):
    """Контроль: уже непустой service_id не перетирается даже если
    повторный вызов принёс другой сервис."""
    first_seen = datetime(2026, 5, 12, 10, 0)
    svc_a = upsert_service(db, "squad-1", "svc-a")
    e1 = record_pod_event(
        db, svc_a, "squad-1", "pod-x", "CrashLoopBackOff",
        event_uid="uid-2", first_seen=first_seen,
    )
    assert e1.service_id == svc_a.id

    svc_b = upsert_service(db, "squad-1", "svc-b")
    e2 = record_pod_event(
        db, svc_b, "squad-1", "pod-x", "CrashLoopBackOff",
        event_uid="uid-2", first_seen=first_seen,
    )
    assert e2.id == e1.id
    assert e2.service_id == svc_a.id  # не перетёрт на svc_b


# ---------- queries -------------------------------------------------------

def test_recent_deploys_within_window(db):
    svc = upsert_service(db, "squad-1", "town-service")
    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    # деплой за 15 мин до инцидента
    record_deployment(
        db, svc,
        started_at=incident_at.replace(tzinfo=None) - timedelta(minutes=15),
        sha="abc",
    )
    # деплой за 8 часов — не должен попасть
    record_deployment(
        db, svc,
        started_at=incident_at.replace(tzinfo=None) - timedelta(hours=8),
        sha="old",
    )

    deploys = recent_deploys_for(
        db, "squad-1", "town-service", before=incident_at, lookback_minutes=60
    )
    assert len(deploys) == 1
    assert deploys[0]["sha"] == "abc"
    assert deploys[0]["minutes_before_incident"] == 15


def test_recent_deploys_unknown_service(db):
    """Сервиса в графе нет — возвращаем [] (≠ ошибка)."""
    out = recent_deploys_for(
        db, "squad-1", "nope", before=datetime.now(timezone.utc)
    )
    assert out == []


def test_upstream_of(db):
    api = upsert_service(db, "squad-1", "api")
    auth = upsert_service(db, "squad-1", "auth")
    db_svc = upsert_service(db, "squad-1", "postgres")
    upsert_edge(db, api, auth, kind="calls", weight=10)  # api → auth
    upsert_edge(db, api, db_svc, kind="reads_from", weight=5)  # api → postgres

    # upstream(api) = от кого api зависит = auth и postgres.
    upstream = upstream_of(db, "squad-1", "api")
    services = {u["service"] for u in upstream}
    assert services == {"auth", "postgres"}


def test_upstream_of_filtered_by_kind(db):
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    upsert_edge(db, a, b, kind="calls")
    upsert_edge(db, a, b, kind="reads_from")

    # upstream(a) с фильтром по calls — только b через calls.
    only_calls = upstream_of(db, "squad-1", "a", kinds=["calls"])
    assert len(only_calls) == 1
    assert only_calls[0]["service"] == "b"


def test_incidents_on_window(db):
    svc = upsert_service(db, "squad-1", "x")
    base = datetime(2026, 5, 12, 10, 0)
    record_alert_event(db, svc, "A1", "warning", "fp-a", base)
    record_alert_event(db, svc, "A2", "critical", "fp-b", base + timedelta(minutes=5))
    record_alert_event(db, svc, "OLD", "warning", "fp-c", base - timedelta(hours=24))

    rows = incidents_on(
        db, "squad-1", "x",
        since=base - timedelta(minutes=10),
        until=base + timedelta(minutes=30),
    )
    fp = {r["fingerprint"] for r in rows}
    assert fp == {"fp-a", "fp-b"}


def test_nearby_alerts_finds_upstream_in_window(db):
    """Боевой кейс: alert на town-service, ищем upstream-инциденты в ±15 мин."""
    town = upsert_service(db, "squad-1", "town-service")
    auth = upsert_service(db, "squad-1", "auth")
    db_svc = upsert_service(db, "squad-1", "postgres")
    # town вызывает auth и читает из postgres
    upsert_edge(db, town, auth, kind="calls", weight=10)
    upsert_edge(db, town, db_svc, kind="reads_from", weight=5)

    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    # auth упал за 3 минуты до инцидента town
    record_alert_event(
        db, auth, "AuthLatency", "warning", "fp-auth",
        incident_at.replace(tzinfo=None) - timedelta(minutes=3),
    )
    # postgres — был alert за 30 минут (вне окна ±15)
    record_alert_event(
        db, db_svc, "DiskFull", "critical", "fp-pg",
        incident_at.replace(tzinfo=None) - timedelta(minutes=30),
    )

    nearby = nearby_alerts(
        db, "squad-1", "town-service", around=incident_at, window_minutes=15
    )
    services = {n["service"] for n in nearby}
    assert services == {"auth"}  # postgres вне окна
    assert nearby[0]["minutes_before"] == 3
    assert nearby[0]["edge_kind"] == "calls"


def test_nearby_alerts_no_upstream(db):
    """Сервис без upstream — пустой результат."""
    upsert_service(db, "squad-1", "isolated")
    out = nearby_alerts(
        db, "squad-1", "isolated", around=datetime.now(timezone.utc)
    )
    assert out == []


# ---------- SimilarIncidentEngine quality gate ----------------------------

def test_is_quality_cause_accepts_real_cause():
    from app.core.intelligence.similar_incidents import _is_quality_cause
    assert _is_quality_cause("SIGSEGV crash in notificator", None) is True
    assert _is_quality_cause("OOM: memory limit exceeded", "resolved") is True


def test_is_quality_cause_rejects_none():
    from app.core.intelligence.similar_incidents import _is_quality_cause
    assert _is_quality_cause(None, None) is False
    assert _is_quality_cause(None, "resolved") is False


def test_is_quality_cause_rejects_unresolved_quality():
    from app.core.intelligence.similar_incidents import _is_quality_cause
    assert _is_quality_cause("Something happened", "unresolved") is False


def test_is_quality_cause_rejects_no_survivor_text():
    """Backward compat: старые записи с полным текстом pipeline-статуса."""
    from app.core.intelligence.similar_incidents import _is_quality_cause
    assert _is_quality_cause(
        "No hypothesis survived adversarial critique. Observed facts: ['crashloop'].",
        None
    ) is False


def test_is_quality_cause_rejects_empty_string():
    from app.core.intelligence.similar_incidents import _is_quality_cause
    assert _is_quality_cause("", None) is False


# ---------- SimilarIncidentEngine recurrence detection -------------------

def test_extract_service_ns_new_format():
    from app.core.intelligence.similar_incidents import _extract_service_ns
    data = {"namespace": "prod-k1", "labels": {"service": "notificator"}}
    svc, ns = _extract_service_ns(data)
    assert svc == "notificator"
    assert ns == "prod-k1"


def test_extract_service_ns_old_format():
    from app.core.intelligence.similar_incidents import _extract_service_ns
    data = {"targets": [{"service": "town-service", "namespace": "preprod-shared"}]}
    svc, ns = _extract_service_ns(data)
    assert svc == "town-service"
    assert ns == "preprod-shared"


def test_extract_service_ns_empty():
    from app.core.intelligence.similar_incidents import _extract_service_ns
    svc, ns = _extract_service_ns({})
    assert svc is None
    assert ns is None


def test_recurrence_flag_set_for_recent_resolved_same_service(db):
    """Инцидент того же сервиса, resolved < 7 дней — recurrence=True."""
    from datetime import datetime, timedelta
    from app.core.intelligence.similar_incidents import SimilarIncidentEngine
    from app.database import IncidentRecord

    recent_incident = IncidentRecord(
        incident_id="old-notificator-1",
        status="RESOLVED",
        is_accepted="ACCEPTED",
        data={"labels": {"service": "notificator"}, "namespace": "squad-10-shared"},
        analysis={
            "cause": "SIGSEGV crash in native interop",
            "resolution_quality": "resolved",
        },
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    db.add(recent_incident)
    db.commit()

    # Патчим SessionLocal чтобы использовать тестовую DB.
    import app.core.intelligence.similar_incidents as sie_module
    original_session = sie_module.SessionLocal
    sie_module.SessionLocal = lambda: db
    try:
        results = SimilarIncidentEngine.find(
            current_incident={
                "labels": {"service": "notificator"},
                "namespace": "squad-10-shared",
            },
            limit=3,
        )
    finally:
        sie_module.SessionLocal = original_session

    assert len(results) >= 1
    match = next(r for r in results if r["incident_id"] == "old-notificator-1")
    assert match["recurrence"] is True
    assert match["days_ago"] == 2


def test_recurrence_false_for_old_resolved_same_service(db):
    """Инцидент того же сервиса, resolved > 7 дней — recurrence=False."""
    from datetime import datetime, timedelta
    from app.core.intelligence.similar_incidents import SimilarIncidentEngine
    from app.database import IncidentRecord

    old_incident = IncidentRecord(
        incident_id="old-notificator-stale",
        status="RESOLVED",
        is_accepted="ACCEPTED",
        data={"labels": {"service": "notificator"}, "namespace": "squad-10-shared"},
        analysis={
            "cause": "SIGSEGV crash in native interop",
            "resolution_quality": "resolved",
        },
        created_at=datetime.utcnow() - timedelta(days=14),
    )
    db.add(old_incident)
    db.commit()

    import app.core.intelligence.similar_incidents as sie_module
    original_session = sie_module.SessionLocal
    sie_module.SessionLocal = lambda: db
    try:
        results = SimilarIncidentEngine.find(
            current_incident={
                "labels": {"service": "notificator"},
                "namespace": "squad-10-shared",
            },
            limit=3,
        )
    finally:
        sie_module.SessionLocal = original_session

    match = next((r for r in results if r["incident_id"] == "old-notificator-stale"), None)
    if match:
        assert match["recurrence"] is False


# ---------- log_error_rate_for (Seq-derived proxy) ------------------------

def _add_log_obs(db, svc, *, ts, level, count, source="prod"):
    db.add(LogObservation(
        service_id=svc.id, ts=ts, level=level, count=count, source=source,
        namespace=svc.namespace,
    ))
    db.commit()


def test_log_error_rate_unknown_service(db):
    """Сервиса в графе нет — None (не путать с «тихо»)."""
    assert log_error_rate_for(db, "squad-1", "nope", now=datetime(2026, 6, 6, 8, 0)) is None


def test_log_error_rate_no_observations_is_zero(db):
    """Сервис есть, наблюдений нет — нули, не None."""
    upsert_service(db, "prod-kingdom5", "town-service")
    out = log_error_rate_for(db, "prod-kingdom5", "town-service",
                             now=datetime(2026, 6, 6, 8, 0))
    assert out is not None
    assert out["error_count"] == 0
    assert out["log_error_rate_per_min"] == 0.0
    assert out["buckets"] == 0
    assert out["is_proxy"] is True


def test_log_error_rate_sums_error_and_fatal_in_window(db):
    svc = upsert_service(db, "prod-kingdom5", "town-service")
    now = datetime(2026, 6, 6, 8, 0)
    # В окне 60 мин: Error=10 + Fatal=2 = 12 событий → 12/60 = 0.2/min.
    _add_log_obs(db, svc, ts=now - timedelta(minutes=10), level="Error", count=10)
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Fatal", count=2)
    # Warning по умолчанию НЕ учитывается.
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Warning", count=999)

    out = log_error_rate_for(db, "prod-kingdom5", "town-service",
                             window_minutes=60, now=now)
    assert out["error_count"] == 12
    assert out["log_error_rate_per_min"] == 0.2
    assert out["buckets"] == 2
    assert out["levels"] == ["Error", "Fatal"]


def test_log_error_rate_excludes_out_of_window(db):
    svc = upsert_service(db, "prod-kingdom5", "town-service")
    now = datetime(2026, 6, 6, 8, 0)
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Error", count=6)
    # За пределами 60-мин окна — не учитывается.
    _add_log_obs(db, svc, ts=now - timedelta(minutes=120), level="Error", count=100)

    out = log_error_rate_for(db, "prod-kingdom5", "town-service",
                             window_minutes=60, now=now)
    assert out["error_count"] == 6
    assert out["buckets"] == 1


def test_log_error_rate_does_not_touch_http_5xx(db):
    """Семантический guard: helper не трогает kg_service_health и не
    выдаёт себя за HTTP 5xx — только log-proxy с явным маркером."""
    svc = upsert_service(db, "prod-kingdom5", "town-service")
    now = datetime(2026, 6, 6, 8, 0)
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Error", count=3)
    out = log_error_rate_for(db, "prod-kingdom5", "town-service", now=now)
    # ключ http_5xx_rate тут отсутствует — мы НЕ маскируемся под него.
    assert "http_5xx_rate" not in out
    assert out["is_proxy"] is True


def test_log_error_rate_custom_levels(db):
    svc = upsert_service(db, "prod-kingdom5", "town-service")
    now = datetime(2026, 6, 6, 8, 0)
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Error", count=4)
    _add_log_obs(db, svc, ts=now - timedelta(minutes=5), level="Warning", count=20)
    out = log_error_rate_for(db, "prod-kingdom5", "town-service",
                             levels=["Warning"], now=now)
    assert out["error_count"] == 20
    assert out["levels"] == ["Warning"]
