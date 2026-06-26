"""Тесты на KG schema/quality contract (`app.knowledge_graph.contract`).

In-memory SQLite используется для STARTUP_CONTRACT_CHECK — поднимаем
Base.metadata и наливаем фикстурные данные через ORM.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.contract import (
    ALL_NODE_KINDS,
    EDGE_KINDS,
    KG_SCHEMA_VERSION,
    OWNER_SOURCES,
    QUALITY_THRESHOLDS,
    REAL_SERVICE_KINDS,
    SERVICE_KINDS,
    STALE_CLASS_EXPECTED_STALE,
    STARTUP_CONTRACT_CHECK,
    SYNTHETIC_KINDS,
    active_edge_kinds,
    compute_orphan_stats,
    is_edge_kind_known,
    is_orphan,
    is_synthetic,
    owner_known,
    planned_edge_kinds,
    service_kind_of,
)
from app.knowledge_graph.schema import Service, ServiceEdge


# ---------------------------------------------------------------------------
# Static / constant checks
# ---------------------------------------------------------------------------

def test_schema_version_non_empty_and_semver_like():
    assert KG_SCHEMA_VERSION
    assert isinstance(KG_SCHEMA_VERSION, str)
    # major.minor format
    parts = KG_SCHEMA_VERSION.split(".")
    assert len(parts) >= 2, f"expected major.minor, got {KG_SCHEMA_VERSION!r}"
    assert all(p.isdigit() for p in parts[:2])


def test_edge_kinds_contains_all_active_known_in_master():
    """Все edge kinds, которые сейчас реально пишутся sync-ами в master,
    должны присутствовать в EDGE_KINDS со status='active'.

    После PR #82/#84/#86 (k8s_jobs / k8s_storage / stale_class) три
    ранее planned kind-а промоутнуты в active.
    """
    must_have_active = {
        "calls", "uses_db", "uses_nats",
        "serves_traffic", "routes_to",
        "runs_as_job", "uses_volume", "bound_to",
    }
    for kind in must_have_active:
        assert kind in EDGE_KINDS, f"edge kind {kind!r} отсутствует в EDGE_KINDS"
        assert EDGE_KINDS[kind]["status"] == "active", (
            f"edge kind {kind!r} должен быть active, а не {EDGE_KINDS[kind]['status']!r}"
        )


def test_no_stale_planned_kinds_after_merges():
    """После merge PR #82/#84/#86 ни один из этих kinds не должен оставаться
    в `planned` — иначе drift между контрактом и реальностью.
    """
    planned = planned_edge_kinds()
    for kind in ("runs_as_job", "uses_volume", "bound_to"):
        assert kind not in planned, (
            f"{kind!r} реализован в master, но остался planned — это drift"
        )


def test_edge_kind_spec_shape():
    """Каждая запись EDGE_KINDS имеет полный набор полей, включая `table`
    (где edge физически живёт: kg_service_edges / kg_volume_edges /
    fk_only / metadata_only).
    """
    required_keys = {"semantic", "src_kinds", "dst_kinds", "source", "example", "status", "table"}
    valid_tables = {"kg_service_edges", "kg_volume_edges", "fk_only", "metadata_only"}
    for kind, spec in EDGE_KINDS.items():
        assert required_keys.issubset(spec.keys()), (
            f"edge kind {kind!r} спека неполная: missing={required_keys - set(spec.keys())}"
        )
        assert spec["status"] in {"active", "planned"}, (
            f"unknown status {spec['status']!r} for {kind!r}"
        )
        assert spec["table"] in valid_tables, (
            f"{kind!r}: unknown table {spec['table']!r}"
        )
        # src/dst kinds — подмножество ALL_NODE_KINDS (service-kinds ∪
        # storage-node-kinds). uses_volume/bound_to живут в namespace
        # `service`/`pvc`/`pv`, остальные — в SERVICE_KINDS.
        assert spec["src_kinds"].issubset(ALL_NODE_KINDS), (
            f"{kind}.src_kinds содержит неизвестный kind: "
            f"{spec['src_kinds'] - ALL_NODE_KINDS}"
        )
        assert spec["dst_kinds"].issubset(ALL_NODE_KINDS), (
            f"{kind}.dst_kinds содержит неизвестный kind: "
            f"{spec['dst_kinds'] - ALL_NODE_KINDS}"
        )


def test_service_kinds_partition():
    """SERVICE_KINDS = REAL_SERVICE_KINDS ⊔ SYNTHETIC_KINDS, без пересечений."""
    assert REAL_SERVICE_KINDS & SYNTHETIC_KINDS == set()
    assert SERVICE_KINDS == REAL_SERVICE_KINDS | SYNTHETIC_KINDS


def test_owner_sources_has_known_provenance():
    must_have = {"manual", "k8s_labels", "namespace_prefix"}
    assert must_have.issubset(OWNER_SOURCES)


def test_quality_thresholds_sensible():
    assert 0 < QUALITY_THRESHOLDS["orphan_rate_max_pct"] < 100
    assert 0 < QUALITY_THRESHOLDS["owner_coverage_min_pct"] <= 100
    assert 0 < QUALITY_THRESHOLDS["sha_coverage_min_pct"] < 100


def test_is_edge_kind_known():
    assert is_edge_kind_known("calls")
    assert is_edge_kind_known("uses_db")
    assert not is_edge_kind_known("totally-bogus-kind")


def test_active_and_planned_are_disjoint():
    assert active_edge_kinds() & planned_edge_kinds() == set()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _svc(*, name="x", namespace="ns", synthetic=False, team_owner=None, svc_id=1):
    s = Service(name=name, namespace=namespace, team_owner=team_owner)
    s.synthetic = synthetic
    s.id = svc_id
    return s


def test_is_synthetic_by_flag():
    s = _svc(synthetic=True)
    assert is_synthetic(s)


def test_is_synthetic_by_name_prefix():
    """Legacy-rows без флага но с synthetic-префиксом."""
    s = _svc(name="ingress:example.com", synthetic=False)
    assert is_synthetic(s)

    s2 = _svc(name="subject:march-export", synthetic=False)
    assert is_synthetic(s2)

    s3 = _svc(name="db:postgres:my-host", synthetic=False)
    assert is_synthetic(s3)


def test_is_synthetic_false_for_real_service():
    s = _svc(name="town-service", synthetic=False)
    assert not is_synthetic(s)


def test_service_kind_of():
    assert service_kind_of(_svc(name="ingress:foo.com")) == "ingress"
    assert service_kind_of(_svc(name="subject:bar")) == "subject"
    assert service_kind_of(_svc(name="db:postgres:host")) == "db"
    # Real service — default deployment
    assert service_kind_of(_svc(name="town-service")) == "deployment"


def test_is_orphan_real_no_edges_is_orphan():
    s = _svc(name="town-service", svc_id=42)
    assert is_orphan(s, edge_ids_seen=[])


def test_is_orphan_real_with_edge_is_not():
    s = _svc(name="town-service", svc_id=42)
    assert not is_orphan(s, edge_ids_seen=[42, 99])


def test_is_orphan_synthetic_never_orphan():
    s = _svc(name="ingress:foo.com", synthetic=True, svc_id=7)
    assert not is_orphan(s, edge_ids_seen=[])


def test_is_orphan_with_recent_deploy_excluded():
    """Сервис без edges, но с deploy за 30d — не orphan."""
    s = _svc(name="town-service", svc_id=42)
    assert not is_orphan(s, edge_ids_seen=[], has_recent_deploy=True)


def test_is_orphan_expected_stale_excluded():
    """expected_stale-инфра (edge-less by design) — не orphan (v2.3)."""
    s = _svc(name="postgres-squad-1", svc_id=42)
    assert not is_orphan(s, edge_ids_seen=[], is_expected_stale=True)


def test_owner_known_basic():
    assert owner_known(_svc(team_owner="squad-1"))
    assert not owner_known(_svc(team_owner=None))
    assert not owner_known(_svc(team_owner=""))
    assert not owner_known(_svc(team_owner="unknown"))
    assert not owner_known(_svc(team_owner="N/A"))


# ---------------------------------------------------------------------------
# STARTUP_CONTRACT_CHECK against in-memory DB
# ---------------------------------------------------------------------------

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


def test_startup_contract_check_empty_db_returns_report(db):
    report = STARTUP_CONTRACT_CHECK(db)
    assert report["schema_version"] == KG_SCHEMA_VERSION
    assert report["unknown_edge_kinds"] == []
    assert report["planned_in_db"] == []


def test_startup_contract_check_flags_unknown_kind(db):
    # Заводим 2 сервиса + edge с unknown kind
    a = Service(name="a", namespace="ns", team_owner="t")
    a.synthetic = False
    b = Service(name="b", namespace="ns", team_owner="t")
    b.synthetic = False
    db.add_all([a, b])
    db.flush()
    edge = ServiceEdge(
        src_id=a.id, dst_id=b.id, kind="bogus_kind_xyz", weight=1,
    )
    db.add(edge)
    db.commit()

    report = STARTUP_CONTRACT_CHECK(db)
    assert "bogus_kind_xyz" in report["unknown_edge_kinds"]


def test_startup_contract_check_flags_planned_present(db, monkeypatch):
    """Если planned kind уже встречается в БД — должен попасть в planned_in_db.

    После promotion `runs_as_job`/`uses_volume`/`bound_to` в active никаких
    planned kinds в EDGE_KINDS сейчас нет, поэтому инжектим временный
    planned-маркер через monkeypatch, чтобы проверить механизм.
    """
    import app.knowledge_graph.contract as contract_mod
    fake_planned: contract_mod.EdgeKindSpec = {
        "semantic": "test-only planned kind",
        "src_kinds": contract_mod.REAL_SERVICE_KINDS,
        "dst_kinds": contract_mod.REAL_SERVICE_KINDS,
        "source": "test",
        "example": "x → y",
        "status": "planned",
        "table": "kg_service_edges",
    }
    patched_kinds = {**contract_mod.EDGE_KINDS, "__test_planned__": fake_planned}
    monkeypatch.setattr(contract_mod, "EDGE_KINDS", patched_kinds)

    a = Service(name="a", namespace="ns", team_owner="t")
    a.synthetic = False
    b = Service(name="b", namespace="ns", team_owner="t")
    b.synthetic = False
    db.add_all([a, b])
    db.flush()
    edge = ServiceEdge(
        src_id=a.id, dst_id=b.id, kind="__test_planned__", weight=1,
    )
    db.add(edge)
    db.commit()

    report = STARTUP_CONTRACT_CHECK(db)
    assert "__test_planned__" in report["planned_in_db"]
    assert "__test_planned__" not in report["unknown_edge_kinds"]


def test_startup_contract_check_orphan_pct_calculated(db):
    """1 real svc без edges + 1 real svc с edge → orphan_pct = 50%."""
    a = Service(name="a", namespace="ns", team_owner="t")
    a.synthetic = False
    b = Service(name="b", namespace="ns", team_owner="t")
    b.synthetic = False
    c = Service(name="orphan", namespace="ns", team_owner="t")
    c.synthetic = False
    db.add_all([a, b, c])
    db.flush()
    db.add(ServiceEdge(src_id=a.id, dst_id=b.id, kind="calls", weight=1))
    db.commit()

    report = STARTUP_CONTRACT_CHECK(db)
    # 3 real, 1 orphan (c) — 33%; a и b связаны edge'ом
    assert report["orphan_pct"] is not None
    assert 30.0 <= report["orphan_pct"] <= 35.0


def test_compute_orphan_stats_app_scope_and_orphan(db):
    """Canonical helper: synthetic скрыт, expected_stale вне scope,
    edge-less active → orphan, с edge → не orphan.

    Seed:
      * syn — synthetic (вне scope, вне orphan)
      * infra — expected_stale без edge (вне scope, вне orphan)
      * orphan-svc — active без edge → orphan
      * linked — active с edge → не orphan
    app_scope = {orphan-svc, linked} = 2; orphan = 1 → 50%.
    """
    syn = Service(name="ingress:foo.com", namespace="ns", team_owner="t")
    syn.synthetic = True
    infra = Service(name="postgres-x", namespace="ns", team_owner="t")
    infra.synthetic = False
    infra.stale_class = STALE_CLASS_EXPECTED_STALE
    orphan_svc = Service(name="orphan-svc", namespace="ns", team_owner="t")
    orphan_svc.synthetic = False
    linked = Service(name="linked", namespace="ns", team_owner="t")
    linked.synthetic = False
    db.add_all([syn, infra, orphan_svc, linked])
    db.flush()
    # cross-node ребро (linked → infra): linked связан, значит non-orphan.
    db.add(ServiceEdge(src_id=linked.id, dst_id=infra.id, kind="uses_db", weight=1))
    db.commit()

    stats = compute_orphan_stats(db)
    assert stats["app_scope"] == 2
    assert stats["orphan"] == 1
    assert stats["orphan_pct"] == 50.0


def test_compute_orphan_stats_self_loop_is_orphan(db):
    """Сервис, у которого ЕДИНСТВЕННОЕ ребро — self-loop (src==dst), —
    топологически изолирован → orphan (guard против возврата маски #190)."""
    only_self = Service(name="self-only", namespace="ns", team_owner="t")
    only_self.synthetic = False
    db.add(only_self)
    db.flush()
    db.add(ServiceEdge(src_id=only_self.id, dst_id=only_self.id,
                       kind="serves_traffic", weight=1))
    db.commit()

    stats = compute_orphan_stats(db)
    assert stats["app_scope"] == 1
    assert stats["orphan"] == 1  # self-loop НЕ спасает от orphan
    assert stats["orphan_pct"] == 100.0


def test_compute_orphan_stats_empty_db_pct_none(db):
    """Пустой scope → orphan_pct = None (не ложный 0%)."""
    stats = compute_orphan_stats(db)
    assert stats["app_scope"] == 0
    assert stats["orphan"] == 0
    assert stats["orphan_pct"] is None
