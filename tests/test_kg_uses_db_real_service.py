"""uses_db ведёт в настоящий k8s Service базы, а не в синтетический `db:`-узел.

До 16.10.2026 рёбра uses_db строились только в `db:<driver>:<hint>` в
`<realm>-shared`, и у k8s Service баз не было ни одного ребра: на проде 62 из
85 орфанов были базами, а blast radius от StatefulSet базы до клиентов не
доходил. Здесь — резолв hint → Service и замена старого ребра.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.knowledge_graph.kg_sync as kg_sync
from app.database import Base
from app.knowledge_graph.kg_sync import (
    _parse_db_hint_from_secret_key,
    _resolve_db_service,
    sync_namespace,
)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import Service, ServiceEdge


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    kg_sync._reset_existing_ns_cache()
    try:
        yield s
    finally:
        kg_sync._reset_existing_ns_cache()
        s.close()
        engine.dispose()


def _k8s_svc(db, ns, name, *, synthetic=False, stale_class=None, seen_hours_ago=0):
    """Узел так, как его пишет k8s_topology_resources_sync из живого Service:
    metadata.k8s_service + ребро serves_traffic в backing workload, которое
    синк освежает, пока Service есть в кластере."""
    svc = upsert_service(
        db, ns, name,
        metadata={"k8s_service": {"service_type": "ClusterIP"}},
        synthetic=synthetic,
    )
    if stale_class is not None:
        svc.stale_class = stale_class
    workload = upsert_service(db, ns, name, node_kind="workload")
    edge = upsert_edge(db, svc, workload, kind="serves_traffic")
    edge.last_seen_at = datetime.utcnow() - timedelta(hours=seen_hours_ago)
    db.flush()
    return svc


def _deploy(name, *keys):
    env = [
        {"name": k, "valueFrom": {"secretKeyRef": {"name": "database", "key": k}}}
        for k in keys
    ]
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"containers": [{"env": env}]}}},
    }


def _uses_db(db, src_name):
    rows = (
        db.query(ServiceEdge, Service)
        .join(Service, Service.id == ServiceEdge.dst_id)
        .filter(ServiceEdge.kind == "uses_db")
        .all()
    )
    out = {}
    for edge, dst in rows:
        src = db.get(Service, edge.src_id)
        if src.name == src_name:
            out[(dst.namespace, dst.name)] = edge.extras or {}
    return out


# ── резолв hint → Service ────────────────────────────────────────────────────


@pytest.mark.parametrize("key, expected", [
    # Ключи и хосты — из шаблона wo-k8s k8s-install/master/secrets/*/database.yaml.
    ("TOWN_DB_CONNECTION", "town-db-postgresql"),
    ("MV_POSTGRES_DB_CONNECTION", "mv-db-postgresql"),
    ("CHAT_STRUCTURE_DB_CONNECTION", "chat-db-postgresql"),
    ("CHAT_CONFIG_DB_URI", "chat-db-postgresql"),
    ("CHAT_MESSAGE_ADDITIONAL_DB_CONNECTION", "chat-messages-additional-db-postgresql"),
    ("CHAT_MESSAGE_GLOBAL_DB_CONNECTION", "chat-messages-global-db-postgresql"),
    ("MAP_COORDINATOR_DB_CONNECTION", "map-coordinator-db-postgresql"),
])
def test_secret_key_resolves_to_template_service_name(db, key, expected):
    _k8s_svc(db, "prod-kingdom1", expected)
    driver, hint = _parse_db_hint_from_secret_key(key)
    found = _resolve_db_service(db, driver, hint, "prod-kingdom1")
    assert found is not None and found.name == expected


def test_kingdom_local_db_found_in_own_namespace(db):
    """town живёт в королевстве: shared-копия с тем же именем не должна выиграть."""
    _k8s_svc(db, "prod-kingdom1", "town-db-postgresql")
    _k8s_svc(db, "prod-shared", "town-db-postgresql")
    found = _resolve_db_service(db, "postgres", "town", "prod-kingdom1")
    assert (found.namespace, found.name) == ("prod-kingdom1", "town-db-postgresql")


def test_shared_db_falls_back_to_realm_shared(db):
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    found = _resolve_db_service(db, "postgres", "config", "prod-kingdom3")
    assert (found.namespace, found.name) == ("prod-shared", "config-db-postgresql")


def test_postgresql_service_preferred_over_cnpg_rw(db):
    """После CNPG Service `-db-postgresql` переключают на primary — DSN не меняется."""
    _k8s_svc(db, "prod-shared", "config-db-cnpg-rw")
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    found = _resolve_db_service(db, "postgres", "config", "prod-shared")
    assert found.name == "config-db-postgresql"


def test_cnpg_rw_used_when_no_postgresql_service(db):
    _k8s_svc(db, "preprod-shared", "mv-db-cnpg-rw")
    found = _resolve_db_service(db, "postgres", "mv", "preprod-kingdom2")
    assert found.name == "mv-db-cnpg-rw"


def test_deleted_service_loses_to_live_cnpg_rw(db):
    """Удалённый внутри живого namespace Service не должен выигрывать вечно.

    Узел и его metadata.k8s_service остаются после удаления Service; живость
    видна только по свежести serves_traffic (ревью Codex, PR #488).
    """
    _k8s_svc(db, "prod-shared", "config-db-postgresql", seen_hours_ago=60 * 24)
    _k8s_svc(db, "prod-shared", "config-db-cnpg-rw")
    found = _resolve_db_service(db, "postgres", "config", "prod-kingdom1")
    assert found.name == "config-db-cnpg-rw"


def test_short_sync_outage_keeps_service(db):
    """Сбой синка на пару часов не должен перекидывать рёбра на синтетику."""
    _k8s_svc(db, "prod-shared", "config-db-postgresql", seen_hours_ago=3)
    assert _resolve_db_service(db, "postgres", "config", "prod-kingdom1") is not None


def test_clickhouse_resolves_by_product_name(db):
    _k8s_svc(db, "prod-shared", "clickhouse")
    found = _resolve_db_service(db, "clickhouse", "analytics", "prod-kingdom2")
    assert (found.namespace, found.name) == ("prod-shared", "clickhouse")


def test_stuck_synthetic_flag_does_not_hide_real_service(db):
    """У части живых Service баз synthetic=true залип — признак реальности k8s_service."""
    _k8s_svc(db, "prod-shared", "config-db-postgresql", synthetic=True)
    assert _resolve_db_service(db, "postgres", "config", "prod-kingdom1") is not None


def test_node_without_k8s_service_metadata_is_ignored(db):
    """Узел без следа живого Service — не цель: ничего не угадываем."""
    upsert_service(db, "prod-shared", "config-db-postgresql")
    db.flush()
    assert _resolve_db_service(db, "postgres", "config", "prod-kingdom1") is None


def test_gone_namespace_service_is_ignored(db):
    _k8s_svc(db, "squad-9-shared", "config-db-postgresql", stale_class="gone")
    assert _resolve_db_service(db, "postgres", "config", "squad-9-kingdom2") is None


def test_unknown_driver_is_not_resolved(db):
    _k8s_svc(db, "prod-shared", "cache-db-postgresql")
    assert _resolve_db_service(db, "redis", "cache", "prod-kingdom1") is None


# ── sync_namespace end-to-end ────────────────────────────────────────────────


def _run_sync(db, monkeypatch, namespace, deploy):
    monkeypatch.setattr(kg_sync, "_refresh_stale_class_for_namespace", lambda *_: 0)
    sync_namespace(db, namespace, deploys=[deploy])
    db.flush()


def test_sync_links_uses_db_to_real_services(db, monkeypatch):
    _k8s_svc(db, "prod-kingdom1", "town-db-postgresql")
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    _k8s_svc(db, "prod-shared", "config-db-cnpg-rw")
    _run_sync(db, monkeypatch, "prod-kingdom1",
              _deploy("town-service", "TOWN_DB_CONNECTION", "CONFIG_DB_CONNECTION"))

    edges = _uses_db(db, "town-service")
    assert set(edges) == {
        ("prod-kingdom1", "town-db-postgresql"),
        ("prod-shared", "config-db-postgresql"),
    }
    town = edges[("prod-kingdom1", "town-db-postgresql")]
    assert town["db_target"] == "k8s_service"
    assert town["db_node"] == "db:postgres:town"
    assert town["db_namespace_source"] == "k8s_service"
    # Синтетика для резолвленных баз не заводится.
    assert db.query(Service).filter(Service.name.like("db:%")).count() == 0


def test_sync_keeps_synthetic_node_when_service_missing(db, monkeypatch):
    """Service не нашёлся — поведение как до резолва: ребро в `db:`-узел."""
    upsert_service(db, "prod-shared", "placeholder")  # чтобы prod-shared существовал
    db.flush()
    _run_sync(db, monkeypatch, "prod-kingdom1",
              _deploy("finance-service-internal", "FINANCE_DB_CONNECTION"))

    edges = _uses_db(db, "finance-service-internal")
    assert set(edges) == {("prod-shared", "db:postgres:finance")}
    assert "db_target" not in edges[("prod-shared", "db:postgres:finance")]


def test_sync_replaces_old_synthetic_edge(db, monkeypatch):
    """Старое ребро клиента в `db:`-узел удаляется сразу, а не через decay.

    Иначе при первом проходе ~27% графа разом уходят в decay, и deadman
    на 25% блокирует его целиком.
    """
    src = upsert_service(db, "prod-kingdom1", "town-service")
    other = upsert_service(db, "prod-kingdom2", "town-service")
    old_db = upsert_service(db, "prod-shared", "db:postgres:config", synthetic=True)
    upsert_edge(db, src, old_db, kind="uses_db")
    upsert_edge(db, other, old_db, kind="uses_db")
    _k8s_svc(db, "prod-shared", "config-db-postgresql")

    _run_sync(db, monkeypatch, "prod-kingdom1",
              _deploy("town-service", "CONFIG_DB_CONNECTION"))

    assert set(_uses_db(db, "town-service")) == {
        ("prod-shared", "config-db-postgresql"),  # новое ребро prod-kingdom1
        ("prod-shared", "db:postgres:config"),     # ребро prod-kingdom2 не тронуто
    }
    remaining_old = db.query(ServiceEdge).filter_by(dst_id=old_db.id).all()
    assert [e.src_id for e in remaining_old] == [other.id]


def test_reset_clears_db_service_cache(db):
    _resolve_db_service(db, "postgres", "town", "prod-kingdom1")  # кэш построен пустым
    _k8s_svc(db, "prod-kingdom1", "town-db-postgresql")
    assert _resolve_db_service(db, "postgres", "town", "prod-kingdom1") is None
    kg_sync._reset_existing_ns_cache()
    assert _resolve_db_service(db, "postgres", "town", "prod-kingdom1") is not None


# ── стоимость прохода: без per-edge запросов на резолв и drop ───────────────


def _count_sql(db, fn):
    """Выполнить fn и вернуть список SQL-операторов, ушедших в БД."""
    from sqlalchemy import event

    statements = []

    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return statements


def _sync_clients(db, monkeypatch, n):
    deploys = [_deploy(f"svc-{i}", "CONFIG_DB_CONNECTION", "TOWN_DB_CONNECTION")
               for i in range(n)]
    monkeypatch.setattr(kg_sync, "_refresh_stale_class_for_namespace", lambda *_: 0)
    sync_namespace(db, "prod-kingdom1", deploys=deploys)
    db.commit()  # как sync_topology: commit per-namespace, объекты протухают


def test_steady_state_pass_has_no_per_edge_resolve_or_drop_queries(db, monkeypatch):
    """Повторный проход: резолв без db.get, drop без SELECT/DELETE.

    16.10.2026 kg_topology_sync вырос с ~2 мин до 817 с: на каждое ребро
    uses_db уходили refresh-SELECT протухшего Service (db.get после commit) и
    SELECT + DELETE на удаление старого ребра в `db:`-узел, которого у
    резолвленного клиента уже нет.
    """
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    _k8s_svc(db, "prod-kingdom1", "town-db-postgresql")
    # Как на проде: `db:`-узел живёт дальше ради клиентов, для которых
    # Service не нашёлся, — старый код ходил в него SELECT + DELETE на ребро.
    old_db = upsert_service(db, "prod-shared", "db:postgres:config", synthetic=True)
    upsert_edge(db, upsert_service(db, "prod-kingdom2", "unresolved"), old_db, kind="uses_db")
    db.commit()
    _sync_clients(db, monkeypatch, 20)
    kg_sync._reset_existing_ns_cache()  # новый проход sync_topology

    sql = _count_sql(db, lambda: _sync_clients(db, monkeypatch, 20))

    deletes = [s for s in sql if s.startswith("DELETE FROM kg_service_edges")]
    assert deletes == []
    # Поиск id `db:`-узла по имени (старый drop): SELECT только id.
    synth_lookups = [s for s in sql if s.startswith(
        "SELECT kg_services.id AS kg_services_id FROM kg_services WHERE")]
    assert synth_lookups == [], synth_lookups[:3]
    by_id = [s for s in sql if s.startswith("SELECT")
             and "FROM kg_services" in s and "WHERE kg_services.id = ?" in s]
    assert by_id == [], by_id[:3]
    # Узлы `db:%` и рёбра в них — по одному запросу на проход, не на ребро.
    synth = [s for s in sql if "LIKE" in s and "kg_service_edges" in s]
    assert len(synth) <= 1


def test_first_pass_still_drops_old_synthetic_edges(db, monkeypatch):
    """Кэш не должен потерять перенос: старые рёбра клиентов в `db:`-узел
    удаляются на первом проходе с резолвом, чужие — остаются."""
    old_db = upsert_service(db, "prod-shared", "db:postgres:config", synthetic=True)
    clients = [upsert_service(db, "prod-kingdom1", f"svc-{i}") for i in range(5)]
    foreign = upsert_service(db, "prod-kingdom2", "svc-x")
    for c in clients + [foreign]:
        upsert_edge(db, c, old_db, kind="uses_db")
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    db.commit()

    deploys = [_deploy(f"svc-{i}", "CONFIG_DB_CONNECTION") for i in range(5)]
    monkeypatch.setattr(kg_sync, "_refresh_stale_class_for_namespace", lambda *_: 0)
    sync_namespace(db, "prod-kingdom1", deploys=deploys)
    db.commit()

    left = db.query(ServiceEdge).filter_by(dst_id=old_db.id, kind="uses_db").all()
    assert [e.src_id for e in left] == [foreign.id]


def test_cache_not_stale_between_separate_sync_namespace_calls(db, monkeypatch):
    """Кэш рёбер в `db:`-узлы живёт один вызов: ребро, появившееся между
    вызовами (или возвращённое откатом savepoint-а), следующий вызов удалит
    (ревью Codex)."""
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    old_db = upsert_service(db, "prod-shared", "db:postgres:config", synthetic=True)
    db.commit()
    deploy = _deploy("svc-0", "CONFIG_DB_CONNECTION")
    monkeypatch.setattr(kg_sync, "_refresh_stale_class_for_namespace", lambda *_: 0)
    sync_namespace(db, "prod-kingdom1", deploys=[deploy])
    db.commit()

    # Между вызовами старое ребро снова появилось (откат, ручная запись).
    client = db.query(Service).filter_by(namespace="prod-kingdom1", name="svc-0",
                                         node_kind="service").one()
    upsert_edge(db, client, old_db, kind="uses_db")
    db.commit()

    sync_namespace(db, "prod-kingdom1", deploys=[deploy])
    db.commit()
    assert db.query(ServiceEdge).filter_by(dst_id=old_db.id, kind="uses_db").count() == 0


def test_rolled_back_drop_is_redone_on_next_call(db, monkeypatch):
    """Savepoint namespace откатился — удалённое ребро вернулось; следующий
    вызов обязан удалить его снова, а не считать уже удалённым."""
    _k8s_svc(db, "prod-shared", "config-db-postgresql")
    old_db = upsert_service(db, "prod-shared", "db:postgres:config", synthetic=True)
    client = upsert_service(db, "prod-kingdom1", "svc-0")
    upsert_edge(db, client, old_db, kind="uses_db")
    db.commit()
    deploy = _deploy("svc-0", "CONFIG_DB_CONNECTION")
    monkeypatch.setattr(kg_sync, "_refresh_stale_class_for_namespace", lambda *_: 0)

    nested = db.begin_nested()
    sync_namespace(db, "prod-kingdom1", deploys=[deploy])
    nested.rollback()
    assert db.query(ServiceEdge).filter_by(dst_id=old_db.id, kind="uses_db").count() == 1

    sync_namespace(db, "prod-kingdom1", deploys=[deploy])
    db.commit()
    assert db.query(ServiceEdge).filter_by(dst_id=old_db.id, kind="uses_db").count() == 0
