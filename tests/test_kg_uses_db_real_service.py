"""uses_db ведёт в настоящий k8s Service базы, а не в синтетический `db:`-узел.

До 16.10.2026 рёбра uses_db строились только в `db:<driver>:<hint>` в
`<realm>-shared`, и у k8s Service баз не было ни одного ребра: на проде 62 из
85 орфанов были базами, а blast radius от StatefulSet базы до клиентов не
доходил. Здесь — резолв hint → Service и замена старого ребра.
"""
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


def _k8s_svc(db, ns, name, *, synthetic=False, stale_class=None):
    """Узел так, как его пишет k8s_topology_resources_sync из живого Service."""
    svc = upsert_service(
        db, ns, name,
        metadata={"k8s_service": {"service_type": "ClusterIP"}},
        synthetic=synthetic,
    )
    if stale_class is not None:
        svc.stale_class = stale_class
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
