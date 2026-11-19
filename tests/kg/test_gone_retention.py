"""Срок жизни узлов stale_class='gone'.

Замер 18.11.2026: 12 342 узла из 24 811 — `gone`, и срока у них не было.
Главное под тестом — чего удаление НЕ делает: не трогает стенд, который
вернулся или может вернуться, не трогает «давно не катившиеся» живые
сервисы и не рвёт историю инцидентов.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.gone_retention import (LIFECYCLE_FRESHNESS,
                                                MIN_GONE_TTL_DAYS,
                                                purge_gone_nodes)
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        Deployment, KGIncident, Namespace,
                                        PodEvent, Service, ServiceEdge,
                                        ServiceHealth, ServiceHealthHourly,
                                        SignalAggregate, VolumeEdge)
from tests.conftest import requires_postgres

NOW = datetime(2026, 11, 18, 4, 25)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ns(db, name, *, state="missing", missing_days=None, seen_ago=timedelta(minutes=5)):
    # last_seen_at задаётся всегда: у колонки default=utcnow, и None здесь
    # превратился бы в «видели только что» — мимо проверки свежести lifecycle.
    gone_for = timedelta(days=missing_days if missing_days is not None else 365)
    db.add(Namespace(
        namespace=name, state=state,
        missing_since=(NOW - timedelta(days=missing_days)) if missing_days is not None else None,
        last_seen_at=(NOW - seen_ago) if state == "active" else (NOW - gone_for),
    ))


def _node(db, node_id, ns, name="svc", stale_class="gone", kind="service"):
    db.add(Service(id=node_id, namespace=ns, name=name, node_kind=kind, stale_class=stale_class))


@pytest.fixture
def graph(db):
    """Живой прод, стенд снесён 40 дней назад, стенд снесён 3 дня назад."""
    _ns(db, "prod-a", state="active")
    _ns(db, "squad-old", missing_days=40)
    _ns(db, "squad-fresh", missing_days=3)
    _node(db, 1, "prod-a", stale_class="active")
    _node(db, 2, "prod-a", name="cron", stale_class="expected_stale")
    _node(db, 3, "prod-a", name="idle", stale_class="suspicious_stale")
    _node(db, 10, "squad-old", name="town")
    _node(db, 11, "squad-old", name="map", kind="workload")
    _node(db, 20, "squad-fresh", name="town")
    db.commit()
    return db


def _run(db, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("now", NOW)
    return purge_gone_nodes(db, **kw)


def _ids(db):
    return sorted(i for (i,) in db.query(Service.id))


# --- что удаляется ----------------------------------------------------------


def test_only_gone_nodes_of_long_missing_namespace_are_deleted(graph):
    stats = _run(graph)

    assert stats["nodes_deleted"] == 2 and stats["namespaces_eligible"] == 1
    assert _ids(graph) == [1, 2, 3, 20]


def test_stale_classes_of_live_services_are_never_touched(graph):
    """expected_stale / suspicious_stale — «давно не катился», а не «не существует»."""
    _run(graph, ttl_days=MIN_GONE_TTL_DAYS)

    left = {s.id: s.stale_class for s in graph.query(Service)}
    assert left[2] == "expected_stale" and left[3] == "suspicious_stale" and left[1] == "active"


def test_observations_and_edges_go_with_the_node(graph):
    db = graph
    old = NOW - timedelta(days=45)
    db.add(ServiceHealth(service_id=10, ts=old, cpu_pct=1.0))
    db.add(ServiceHealthHourly(service_id=10, hour=old.replace(minute=0), samples=6))
    db.add(AnomalyObservation(service_id=10, ts=old, metric="cpu_pct", severity="high"))
    db.add(SignalAggregate(service_id=10, window_end=old))
    db.add(Deployment(service_id=10, sha="1", started_at=old))
    db.add(ServiceEdge(src_id=10, dst_id=11, kind="calls"))
    db.add(ServiceEdge(src_id=1, dst_id=10, kind="calls"))       # с живого конца
    db.add(ServiceEdge(src_id=1, dst_id=2, kind="calls"))        # чужое ребро
    db.add(VolumeEdge(src_kind="service", src_id=10, dst_kind="pvc", dst_id=500, kind="uses_volume"))
    db.add(VolumeEdge(src_kind="pvc", src_id=10, dst_kind="pv", dst_id=11, kind="bound_to"))
    db.add(ServiceHealth(service_id=1, ts=old, cpu_pct=1.0))     # чужая точка
    db.commit()

    stats = _run(db)

    assert stats["rows"]["kg_service_edges"] == 2
    assert stats["rows"]["kg_deployments"] == 1
    assert db.query(ServiceHealth).filter_by(service_id=10).count() == 0
    assert db.query(ServiceHealthHourly).count() == 0
    assert db.query(AnomalyObservation).count() == 0
    assert db.query(SignalAggregate).count() == 0
    assert db.query(Deployment).count() == 0
    edges = [(e.src_id, e.dst_id) for e in db.query(ServiceEdge)]
    assert edges == [(1, 2)], "рёбра удалённого узла ушли с обоих концов, чужое цело"
    vol = [(v.src_kind, v.src_id) for v in db.query(VolumeEdge)]
    assert vol == [("pvc", 10)], "id 10 у pvc — другой объект, не узел графа"
    assert db.query(ServiceHealth).filter_by(service_id=1).count() == 1


def test_incident_and_alert_history_survives_without_the_link(graph):
    """Инцидент хранит namespace и service_name — он остаётся, уходит только ссылка."""
    db = graph
    old = NOW - timedelta(days=45)
    db.add(KGIncident(incident_key="squad-old/town@1", namespace="squad-old", service_name="town",
                      service_id=10, status="resolved", opened_at=old, last_alert_at=old,
                      resolved_at=old))
    db.add(AlertEvent(fingerprint="fp-1", alertname="PodCrashLooping", service_id=10,
                      fired_at=old, resolved_at=old))
    db.add(PodEvent(event_uid="ev-1", namespace="squad-old", pod_name="town-0",
                    reason="BackOff", service_id=10, first_seen=old, last_seen=old))
    db.commit()

    stats = _run(db)

    assert stats["nodes_deleted"] == 2
    inc = db.query(KGIncident).one()
    assert (inc.namespace, inc.service_name, inc.service_id) == ("squad-old", "town", None)
    assert db.query(AlertEvent).one().service_id is None
    assert db.query(PodEvent).one().service_id is None
    assert stats["rows"]["kg_incidents.detached"] == 1


def test_node_with_open_incident_or_firing_alert_is_skipped(graph):
    db = graph
    old = NOW - timedelta(days=45)
    db.add(KGIncident(incident_key="squad-old/town@2", namespace="squad-old", service_name="town",
                      service_id=10, status="open", opened_at=old, last_alert_at=old))
    db.add(AlertEvent(fingerprint="fp-2", alertname="X", service_id=11, fired_at=old))
    db.commit()

    stats = _run(db)

    assert stats["nodes_deleted"] == 0 and stats["nodes_skipped_busy"] == 2
    assert _ids(db) == [1, 2, 3, 10, 11, 20]
    assert db.query(Namespace).get("squad-old").state == "missing", "узлы остались — не retired"


# --- гонка с записью истории (ревью PR #567) --------------------------------


def test_incident_committed_between_check_and_lock_keeps_the_node(graph, monkeypatch):
    """Вебхук открыл инцидент после первой проверки занятости, но до блокировки
    узлов: проверка под блокировкой обязана его увидеть."""
    from app.knowledge_graph import gone_retention as mod

    db = graph
    real_lock = mod._lock_gone
    fired = []

    def lock_after_webhook(db_, ids):
        if not fired:
            fired.append(True)
            old = NOW - timedelta(minutes=1)
            db_.add(KGIncident(incident_key="squad-old/town@race", namespace="squad-old",
                               service_name="town", service_id=10, status="open",
                               opened_at=old, last_alert_at=old))
            db_.add(AlertEvent(fingerprint="fp-race", alertname="X", service_id=10, fired_at=old))
            db_.commit()
        return real_lock(db_, ids)

    monkeypatch.setattr(mod, "_lock_gone", lock_after_webhook)
    stats = _run(db)

    assert stats["nodes_skipped_busy"] == 1 and stats["nodes_deleted"] == 1
    assert _ids(db) == [1, 2, 3, 10, 20], "узел с только что открытым инцидентом цел"
    assert db.query(KGIncident).one().service_id == 10, "живая история не отвязана"
    assert db.query(AlertEvent).one().service_id == 10
    assert db.query(Namespace).get("squad-old").state == "missing"


def test_row_written_during_delete_does_not_stay_dangling(graph, monkeypatch):
    """Таблица без FK (в проде — kg_alerts): строка, записанная после проверки
    под блокировкой, не должна остаться со ссылкой на удалённый узел."""
    from app.knowledge_graph import gone_retention as mod

    db = graph
    real_delete = mod._delete_batch

    def delete_then_late_alert(db_, ids):
        counts = real_delete(db_, ids)
        db_.add(AlertEvent(fingerprint="fp-late", alertname="X", service_id=10,
                           fired_at=NOW))
        return counts

    monkeypatch.setattr(mod, "_delete_batch", delete_then_late_alert)
    stats = _run(db)

    assert stats["nodes_deleted"] == 2 and stats["late_refs"] == 1
    assert db.query(AlertEvent).one().service_id is None
    assert _ids(db) == [1, 2, 3, 20]


def test_no_late_refs_in_the_ordinary_run(graph):
    assert _run(graph)["late_refs"] == 0


# --- что может вернуться ----------------------------------------------------


def test_returned_namespace_is_not_touched(graph):
    """Стенд вернулся: lifecycle перевёл его в active и снял missing_since."""
    db = graph
    row = db.query(Namespace).get("squad-old")
    row.state, row.missing_since, row.last_seen_at = "active", None, NOW
    db.commit()

    stats = _run(db)

    assert stats["nodes_deleted"] == 0 and _ids(db) == [1, 2, 3, 10, 11, 20]


def test_ttl_counts_from_missing_since_not_from_node_age(graph):
    """Узел старый, но стенд пропал недавно — срок ещё не вышел."""
    db = graph
    db.query(Service).filter_by(id=20).update(
        {"created_at": NOW - timedelta(days=200), "updated_at": NOW - timedelta(days=200)})
    db.commit()

    _run(db)

    assert 20 in _ids(db)


def test_reclassified_node_in_missing_namespace_survives(graph):
    """Класс уже не gone (namespace возвращается прямо сейчас) — узел не наш."""
    db = graph
    db.query(Service).filter_by(id=11).update({"stale_class": "active"})
    db.commit()

    stats = _run(db)

    assert stats["nodes_deleted"] == 1 and 11 in _ids(db)
    assert db.query(Namespace).get("squad-old").state == "missing"


def test_namespace_without_missing_since_is_skipped(graph):
    db = graph
    _ns(db, "squad-unknown", missing_days=None)
    _node(db, 30, "squad-unknown")
    db.commit()

    _run(db)

    assert 30 in _ids(db)


def test_stale_lifecycle_blocks_the_run(db):
    """Lifecycle не ходил — отметкам missing_since верить нельзя: стенд мог вернуться."""
    _ns(db, "prod-a", state="active", seen_ago=LIFECYCLE_FRESHNESS + timedelta(minutes=1))
    _ns(db, "squad-old", missing_days=40)
    _node(db, 10, "squad-old")
    db.commit()

    stats = _run(db)

    assert stats["skipped"] == "lifecycle_stale" and stats["nodes_deleted"] == 0
    assert _ids(db) == [10]


@pytest.mark.parametrize("days", [0, 1, MIN_GONE_TTL_DAYS - 1, -5])
def test_ttl_below_floor_is_refused(graph, days):
    stats = _run(graph, ttl_days=days)

    assert stats["nodes_deleted"] == 0 and "минимума" in stats["skipped"]
    assert _ids(graph) == [1, 2, 3, 10, 11, 20]


# --- режимы и ограничения ---------------------------------------------------


def test_dry_run_is_default_and_writes_nothing(graph):
    db = graph
    db.add(Deployment(service_id=10, sha="1", started_at=NOW - timedelta(days=45)))
    db.commit()

    stats = purge_gone_nodes(db, now=NOW)

    assert stats["dry_run"] is True
    assert stats["would_delete_nodes"] == 2 and stats["nodes_deleted"] == 0
    assert stats["rows"]["kg_deployments"] == 1
    assert _ids(db) == [1, 2, 3, 10, 11, 20]
    assert db.query(Deployment).count() == 1
    assert db.query(Namespace).get("squad-old").state == "missing"


def test_only_namespaces_narrows_the_run(graph):
    db = graph
    _ns(db, "squad-older", missing_days=60)
    _node(db, 40, "squad-older")
    db.commit()

    stats = _run(db, only_namespaces=["squad-older", "squad-fresh", "prod-a"])

    assert stats["nodes_deleted"] == 1
    assert _ids(db) == [1, 2, 3, 10, 11, 20], "список сужает, но срок не отменяет"


def test_run_is_capped_and_resumes(graph):
    db = graph
    for i in range(5):
        _node(db, 100 + i, "squad-old", name=f"extra-{i}")
    db.commit()

    first = _run(db, node_batch=2, max_nodes=4)
    assert first["truncated"] is True and first["nodes_deleted"] == 4
    second = _run(db, node_batch=2, max_nodes=100)
    assert second["truncated"] is False and second["nodes_deleted"] == 3
    assert _ids(db) == [1, 2, 3, 20]


def test_emptied_namespace_becomes_retired_and_keeps_its_row(graph):
    """Строка kg_namespaces остаётся: по ней lifecycle поднимет инкарнацию при возврате."""
    db = graph
    db.query(Namespace).filter_by(namespace="squad-old").update({"incarnation": 3})
    db.commit()

    stats = _run(db)

    assert stats["namespaces_retired"] == 1
    row = db.query(Namespace).get("squad-old")
    assert row.state == "retired" and row.incarnation == 3
    again = _run(db)
    assert again["nodes_deleted"] == 0 and again["namespaces_retired"] == 0


def test_settings_default_is_off():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["KG_GONE_NODES_TTL_MODE"].default == "off"
    assert fields["KG_GONE_NODES_TTL_DAYS"].default >= MIN_GONE_TTL_DAYS
    assert fields["KG_GONE_NODES_TTL_NAMESPACES"].default == ""


def test_every_reference_to_kg_services_is_handled():
    """Новая таблица с FK на kg_services без записи в gone_retention уронила бы
    удаление узла (в свежей базе) или оставила бы висячие строки (в проде)."""
    from app.knowledge_graph import gone_retention as mod

    handled = {t for t, _ in mod._DELETE_WITH_NODE} | set(mod._DETACH_FROM_NODE)
    referencing = {
        table.name
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "kg_services"
    }
    assert referencing <= handled, f"не обработаны: {sorted(referencing - handled)}"


# --- PostgreSQL: настоящие FK и блокировки ----------------------------------


@pytest.fixture
def pg_session():
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    names = ("gone-ttl-test-old", "gone-ttl-test-live")

    def _cleanup():
        session.rollback()
        ids = [i for (i,) in session.query(Service.id).filter(Service.namespace.in_(names))]
        if ids:
            for table in ("kg_deployments", "kg_service_health", "kg_incidents", "kg_alerts"):
                session.execute(
                    text(f"DELETE FROM {table} WHERE service_id = ANY(:ids)"), {"ids": ids})
            session.execute(text(
                "DELETE FROM kg_service_edges WHERE src_id = ANY(:ids) OR dst_id = ANY(:ids)"),
                {"ids": ids})
        session.execute(text("DELETE FROM kg_incidents WHERE namespace = ANY(:n)"), {"n": list(names)})
        session.execute(text("DELETE FROM kg_alerts WHERE fingerprint = 'gone-ttl-test-fp'"))
        session.execute(text("DELETE FROM kg_services WHERE namespace = ANY(:n)"), {"n": list(names)})
        session.execute(text("DELETE FROM kg_namespaces WHERE namespace = ANY(:n)"), {"n": list(names)})
        session.commit()

    _cleanup()
    try:
        yield session
    finally:
        _cleanup()
        session.close()


@requires_postgres
def test_purge_on_postgres_with_real_foreign_keys(pg_session):
    db = pg_session
    now = datetime.utcnow()
    old = now - timedelta(days=45)
    db.add(Namespace(namespace="gone-ttl-test-live", state="active", last_seen_at=now))
    db.add(Namespace(namespace="gone-ttl-test-old", state="missing",
                     missing_since=now - timedelta(days=40),
                     last_seen_at=now - timedelta(days=40)))
    gone = Service(namespace="gone-ttl-test-old", name="town", node_kind="service", stale_class="gone")
    live = Service(namespace="gone-ttl-test-live", name="town", node_kind="service", stale_class="active")
    db.add_all([gone, live])
    db.flush()
    db.add(Deployment(service_id=gone.id, sha="1", started_at=old))
    db.add(ServiceHealth(service_id=gone.id, ts=old, cpu_pct=1.0))
    db.add(ServiceEdge(src_id=live.id, dst_id=gone.id, kind="calls"))
    db.add(KGIncident(incident_key="gone-ttl-test-old/town@1", namespace="gone-ttl-test-old",
                      service_name="town", service_id=gone.id, status="resolved",
                      opened_at=old, last_alert_at=old, resolved_at=old))
    db.add(AlertEvent(fingerprint="gone-ttl-test-fp", alertname="X", service_id=gone.id,
                      fired_at=old, resolved_at=old))
    db.commit()
    gone_id, live_id = gone.id, live.id
    only = ["gone-ttl-test-old", "gone-ttl-test-live"]

    dry = purge_gone_nodes(db, only_namespaces=only, now=now)
    assert dry["would_delete_nodes"] == 1 and dry["rows"]["kg_deployments"] == 1

    stats = purge_gone_nodes(db, dry_run=False, only_namespaces=only, now=now)

    assert stats["nodes_deleted"] == 1 and stats["namespaces_retired"] == 1
    db.expire_all()
    assert db.query(Service).filter_by(id=gone_id).count() == 0
    assert db.query(Service).filter_by(id=live_id).count() == 1
    assert db.query(Deployment).filter_by(service_id=gone_id).count() == 0
    assert db.query(ServiceEdge).filter_by(dst_id=gone_id).count() == 0
    inc = db.query(KGIncident).filter_by(incident_key="gone-ttl-test-old/town@1").one()
    assert inc.service_id is None and inc.service_name == "town"
    assert db.query(AlertEvent).filter_by(fingerprint="gone-ttl-test-fp").one().service_id is None


@requires_postgres
def test_inflight_incident_insert_blocks_node_delete_on_postgres(pg_session):
    """Настоящая гонка на FK: вебхук вставил открытый инцидент и ещё не
    закоммитил. Вставка держит FOR KEY SHARE на строке узла — SKIP LOCKED узел
    не берёт; после коммита узел занят и пропускается проверкой."""
    from app.database import SessionLocal

    db = pg_session
    now = datetime.utcnow()
    db.add(Namespace(namespace="gone-ttl-test-live", state="active", last_seen_at=now))
    db.add(Namespace(namespace="gone-ttl-test-old", state="missing",
                     missing_since=now - timedelta(days=40),
                     last_seen_at=now - timedelta(days=40)))
    gone = Service(namespace="gone-ttl-test-old", name="town", node_kind="service", stale_class="gone")
    db.add(gone)
    db.commit()
    gone_id = gone.id
    only = ["gone-ttl-test-old"]

    webhook = SessionLocal()
    try:
        webhook.add(KGIncident(incident_key="gone-ttl-test-old/town@race",
                               namespace="gone-ttl-test-old", service_name="town",
                               service_id=gone_id, status="open",
                               opened_at=now, last_alert_at=now))
        webhook.flush()                       # INSERT выполнен, транзакция открыта

        first = purge_gone_nodes(db, dry_run=False, only_namespaces=only, now=now)
        assert first["nodes_deleted"] == 0, "узел под KEY SHARE вставки не взят"

        webhook.commit()
    finally:
        webhook.close()

    second = purge_gone_nodes(db, dry_run=False, only_namespaces=only, now=now)

    assert second["nodes_deleted"] == 0 and second["nodes_skipped_busy"] == 1
    db.expire_all()
    assert db.query(Service).filter_by(id=gone_id).count() == 1
    inc = db.query(KGIncident).filter_by(incident_key="gone-ttl-test-old/town@race").one()
    assert inc.service_id == gone_id
