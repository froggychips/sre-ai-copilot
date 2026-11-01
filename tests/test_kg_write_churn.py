"""Upsert'ы графа не переписывают строку, в которой ничего не изменилось.

01.11.2026: upsert сервисов, рёбер и деплоев давал 48% WAL базы — каждый
проход переписывал строку ради одной метки времени. Проверяем на живом PG
по ctid: новая версия строки (в т.ч. HOT) = новый ctid.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.knowledge_graph import populator
from tests.conftest import requires_postgres


@pytest.fixture
def pg_session():
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctid(db, table, row_id):
    return db.execute(text(f"SELECT ctid FROM {table} WHERE id = :i"), {"i": row_id}).scalar()


def _age(db, table, column, row_id, delta):
    db.execute(text(f"UPDATE {table} SET {column} = {column} - :d WHERE id = :i"),
               {"d": delta, "i": row_id})


@requires_postgres
def test_service_upsert_skips_unchanged_row(pg_session):
    db = pg_session
    svc = populator.upsert_service(db, "churn-ns", "svc-a", team_owner="team-x",
                                   owner_source="k8s_labels", k8s_uid="uid-1")
    before = _ctid(db, "kg_services", svc.id)
    populator.upsert_service(db, "churn-ns", "svc-a", team_owner="team-x",
                             owner_source="k8s_labels", k8s_uid="uid-1")
    assert _ctid(db, "kg_services", svc.id) == before


@requires_postgres
def test_service_upsert_writes_real_changes_and_stale_touch(pg_session):
    db = pg_session
    svc = populator.upsert_service(db, "churn-ns", "svc-b", team_owner="team-x",
                                   owner_source="k8s_labels", k8s_uid="uid-1")
    c0 = _ctid(db, "kg_services", svc.id)
    svc = populator.upsert_service(db, "churn-ns", "svc-b", team_owner="team-y",
                                   owner_source="k8s_labels", k8s_uid="uid-1")
    assert svc.team_owner == "team-y" and _ctid(db, "kg_services", svc.id) != c0
    # Пересоздание объекта — incarnation растёт, как раньше.
    svc = populator.upsert_service(db, "churn-ns", "svc-b", team_owner="team-y",
                                   owner_source="k8s_labels", k8s_uid="uid-2")
    assert svc.incarnation == 2
    # Метка старше интервала — касание обязано пройти.
    _age(db, "kg_services", "updated_at", svc.id,
         populator.SERVICE_TOUCH_INTERVAL + timedelta(minutes=1))
    stale = db.execute(text("SELECT updated_at FROM kg_services WHERE id=:i"),
                       {"i": svc.id}).scalar()
    svc = populator.upsert_service(db, "churn-ns", "svc-b", team_owner="team-y",
                                   owner_source="k8s_labels", k8s_uid="uid-2")
    fresh = db.execute(text("SELECT updated_at FROM kg_services WHERE id=:i"),
                       {"i": svc.id}).scalar()
    assert fresh > stale


@requires_postgres
def test_edge_upsert_touches_by_interval_and_weight(pg_session):
    db = pg_session
    a = populator.upsert_service(db, "churn-ns", "src")
    b = populator.upsert_service(db, "churn-ns", "dst")
    e = populator.upsert_edge(db, src=a, dst=b, kind="calls", discovered_by="s1")
    c0 = _ctid(db, "kg_service_edges", e.id)
    # Повтор и смена discovered_by внутри интервала — строка не трогается,
    # но источник попадает в extras.discovery_sources.
    populator.upsert_edge(db, src=a, dst=b, kind="calls", discovered_by="s1")
    assert _ctid(db, "kg_service_edges", e.id) == c0
    e = populator.upsert_edge(db, src=a, dst=b, kind="calls", discovered_by="s2")
    assert set(e.extras["discovery_sources"]) == {"s1", "s2"}
    # Рост weight пишется сразу.
    e = populator.upsert_edge(db, src=a, dst=b, kind="calls", weight=5)
    assert e.weight == 5
    # Устаревшая метка — касание проходит.
    _age(db, "kg_service_edges", "last_seen_at", e.id,
         populator.EDGE_TOUCH_INTERVAL + timedelta(minutes=1))
    old = db.execute(text("SELECT last_seen_at FROM kg_service_edges WHERE id=:i"),
                     {"i": e.id}).scalar()
    populator.upsert_edge(db, src=a, dst=b, kind="calls", discovered_by="s1")
    new = db.execute(text("SELECT last_seen_at FROM kg_service_edges WHERE id=:i"),
                     {"i": e.id}).scalar()
    assert new > old


@requires_postgres
def test_deployment_upsert_only_backfills_sha(pg_session):
    db = pg_session
    svc = populator.upsert_service(db, "churn-ns", "svc-d")
    started = datetime(2026, 11, 1, 8, 0)
    d = populator.record_deployment(db, svc, started, buildtype_id="Bt", build_number="7")
    c0 = _ctid(db, "kg_deployments", d.id)
    populator.record_deployment(db, svc, started, buildtype_id="Bt", build_number="7")
    assert _ctid(db, "kg_deployments", d.id) == c0
    d = populator.record_deployment(db, svc, started, sha="abc", buildtype_id="Bt",
                                    build_number="7")
    assert d.sha == "abc"
    c1 = _ctid(db, "kg_deployments", d.id)
    d = populator.record_deployment(db, svc, started, sha="zzz", buildtype_id="Bt",
                                    build_number="7")
    assert d.sha == "abc" and _ctid(db, "kg_deployments", d.id) == c1
