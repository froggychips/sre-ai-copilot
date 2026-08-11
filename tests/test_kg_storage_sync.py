"""Tests for KG Coverage #2 — k8s_storage_sync (PVC/PV/storage signals).

Покрываем:
- _parse_capacity_to_bytes: Gi/Mi/Ti/голое число/invalid → None
- _extract_pvc_fields / _extract_pv_fields: правильный сабсет JSON
- _pod_owner_chain_to_deployment: Pod → ReplicaSet → Deployment, и
  напрямую Pod → StatefulSet
- _pod_pvc_claims: dedup; configMap/secret volumes игнорируем
- sync_pvs: upsert идемпотентен (повторный вызов не дубль)
- sync_pvcs: bound_to edge на существующий PV; pending PVC (no PV) не
  создаёт edge; unknown PV → skipped counter
- sync_pod_pvc_edges: 2 PVC на одном pod → 2 edge'а; standalone pod
  без owner → skipped_no_owner
- disk_pct enrichment: mock VM response → disk_pct прикреплён
- идемпотентность сквозная: повторный sync_storage не плодит дубли
- kubectl failure: пустой результат, sync не raise

Третья волна ревью добавила сюда:
- НАБЛЮДАЕМОСТЬ отказа fetch'а: таймаут/rc!=0 больше не выглядит как
  «0 объектов». Он даёт `errors>0` + `fetch_failed` в stats и уходит в
  `record_source_run`, иначе отказ неотличим от «нет данных» (ровно тот
  класс инцидента, что описан в edge_decay_guard).
- chunking + поднятый таймаут для cluster-wide листов (pods/replicasets).
- УБЫЛЬ: decay `kg_volume_edges` по last_seen_at и чистка узлов
  `kg_storage_volumes`, которых нет в снимке кластера. Обе — только при
  здоровом источнике: иначе повторили бы «тихую эрозию рёбер».
"""
import subprocess
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.edge_decay_guard import (
    REASON_EMPTY_FETCH, REASON_FETCH_ERRORS, REASON_NO_RECENT_REFRESH,
    SOURCE_STORAGE_PODS, SOURCE_STORAGE_PVCS, get_source_report,
    record_source_run, reset_source_reports)
from app.knowledge_graph.k8s_storage_sync import (
    DISCOVERED_BY_PODS, DISCOVERED_BY_PVC_SPEC, EDGE_BOUND_TO,
    EDGE_USES_VOLUME, NODE_PV, NODE_PVC, NODE_SERVICE, KubectlFetchError,
    _build_rs_to_deployment_index, _extract_pv_fields, _extract_pvc_fields,
    _kubectl_get_all, _parse_capacity_to_bytes, _pod_owner_chain_to_deployment,
    _pod_pvc_claims, _upsert_volume, _upsert_volume_edge, decay_volume_edges,
    sync_pod_pvc_edges, sync_pvcs, sync_pvs, sync_storage)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import (Service, StorageVolume,  # noqa: F401
                                         VolumeEdge)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_source_reports():
    """Реестр stats-отчётов синков живёт на уровне модуля — чистим между
    тестами, иначе отчёт соседнего теста подменяет здоровье источника."""
    reset_source_reports()
    yield
    reset_source_reports()


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


def _mk_pvc(
    name: str,
    namespace: str,
    storage_class: str = "local-path",
    requested: str = "10Gi",
    phase: str = "Bound",
    volume_name: str = "",
    access_modes=None,
):
    pvc = {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "storageClassName": storage_class,
            "accessModes": access_modes or ["ReadWriteOnce"],
            "resources": {"requests": {"storage": requested}},
        },
        "status": {"phase": phase},
    }
    if volume_name:
        pvc["spec"]["volumeName"] = volume_name
    return pvc


def _mk_pv(
    name: str,
    capacity: str = "10Gi",
    storage_class: str = "local-path",
    phase: str = "Bound",
    claim_ns: str = "",
    claim_name: str = "",
):
    pv = {
        "metadata": {"name": name},
        "spec": {
            "capacity": {"storage": capacity},
            "storageClassName": storage_class,
            "accessModes": ["ReadWriteOnce"],
            "local": {"path": f"/opt/local-path-provisioner/{name}"},
            "persistentVolumeReclaimPolicy": "Delete",
        },
        "status": {"phase": phase},
    }
    if claim_name:
        pv["spec"]["claimRef"] = {"namespace": claim_ns, "name": claim_name}
    return pv


def _mk_pod(
    name: str,
    namespace: str,
    pvc_claims=None,
    owner_kind: str = "ReplicaSet",
    owner_name: str = "",
):
    volumes = []
    for claim in (pvc_claims or []):
        volumes.append({
            "name": f"data-{claim}",
            "persistentVolumeClaim": {"claimName": claim},
        })
    # Добавим один configMap volume — должен быть проигнорирован
    volumes.append({"name": "cfg", "configMap": {"name": "app-config"}})

    owner_refs = []
    if owner_name:
        owner_refs.append({
            "kind": owner_kind, "name": owner_name, "controller": True,
        })
    return {
        "metadata": {
            "name": name, "namespace": namespace,
            "ownerReferences": owner_refs,
        },
        "spec": {"volumes": volumes},
    }


def _mk_rs(name: str, namespace: str, deployment_name: str = ""):
    owner_refs = []
    if deployment_name:
        owner_refs.append({
            "kind": "Deployment", "name": deployment_name, "controller": True,
        })
    return {
        "metadata": {
            "name": name, "namespace": namespace,
            "ownerReferences": owner_refs,
        },
    }


# ── pure helpers: capacity parsing ──────────────────────────────────────────


def test_parse_capacity_gigabyte_binary():
    assert _parse_capacity_to_bytes("100Gi") == 100 * 1024**3


def test_parse_capacity_megabyte_binary():
    assert _parse_capacity_to_bytes("500Mi") == 500 * 1024**2


def test_parse_capacity_terabyte_binary():
    assert _parse_capacity_to_bytes("2Ti") == 2 * 1024**4


def test_parse_capacity_decimal_units():
    """k8s также принимает decimal суффиксы (10G != 10Gi)."""
    assert _parse_capacity_to_bytes("10G") == 10 * 1000**3
    assert _parse_capacity_to_bytes("100M") == 100 * 1000**2


def test_parse_capacity_bare_number():
    assert _parse_capacity_to_bytes("12345") == 12345


def test_parse_capacity_unparseable_returns_none():
    assert _parse_capacity_to_bytes("") is None
    assert _parse_capacity_to_bytes(None) is None
    assert _parse_capacity_to_bytes("garbage") is None
    # exotic exponent format — out of scope per design
    assert _parse_capacity_to_bytes("1e9") is None


# ── pure helpers: extract pvc/pv ────────────────────────────────────────────


def test_extract_pvc_fields_bound():
    pvc = _mk_pvc(
        "data-clickhouse-0", "prod-shared",
        storage_class="local-path", requested="100Gi", phase="Bound",
        volume_name="pvc-abcdef-1234",
    )
    f = _extract_pvc_fields(pvc)
    assert f["kind"] == "pvc"
    assert f["namespace"] == "prod-shared"
    assert f["name"] == "data-clickhouse-0"
    assert f["capacity_bytes"] == 100 * 1024**3
    assert f["storage_class"] == "local-path"
    assert f["phase"] == "Bound"
    assert f["volume_name"] == "pvc-abcdef-1234"
    assert f["access_modes"] == ["ReadWriteOnce"]


def test_extract_pvc_pending_no_volume_name():
    pvc = _mk_pvc("orphan", "dev-1", phase="Pending")
    f = _extract_pvc_fields(pvc)
    assert f["phase"] == "Pending"
    assert f["volume_name"] is None


def test_extract_pvc_bound_status_capacity_overrides_request():
    """PVC попросил 10Gi, провизионер выдал 12Gi → status.capacity.storage."""
    pvc = _mk_pvc("data", "prod-shared", requested="10Gi", phase="Bound")
    pvc["status"]["capacity"] = {"storage": "12Gi"}
    f = _extract_pvc_fields(pvc)
    assert f["capacity_bytes"] == 12 * 1024**3


def test_extract_pv_fields_with_claim_ref():
    pv = _mk_pv(
        "pvc-abc-123", capacity="100Gi", phase="Bound",
        claim_ns="prod-shared", claim_name="data-clickhouse-0",
    )
    f = _extract_pv_fields(pv)
    assert f["kind"] == "pv"
    assert f["namespace"] == ""  # cluster-scoped
    assert f["name"] == "pvc-abc-123"
    assert f["capacity_bytes"] == 100 * 1024**3
    assert f["phase"] == "Bound"
    assert f["metadata_json"]["claim_ref"] == "prod-shared/data-clickhouse-0"
    assert f["metadata_json"]["reclaim_policy"] == "Delete"
    assert f["metadata_json"]["source_type"] == "local"


# ── pure helpers: owner chain ───────────────────────────────────────────────


def test_pod_owner_chain_via_replicaset():
    rs = _mk_rs("auth-7f9c", "prod-shared", deployment_name="auth")
    rs_index = _build_rs_to_deployment_index([rs])

    pod = _mk_pod("auth-7f9c-abcde", "prod-shared",
                  owner_kind="ReplicaSet", owner_name="auth-7f9c")
    assert _pod_owner_chain_to_deployment(pod, rs_index) == ("prod-shared", "auth")


def test_pod_owner_chain_statefulset_direct():
    pod = _mk_pod("clickhouse-0", "prod-shared",
                  owner_kind="StatefulSet", owner_name="clickhouse")
    assert _pod_owner_chain_to_deployment(pod, {}) == ("prod-shared", "clickhouse")


def test_pod_owner_chain_orphan_rs_returns_rs_name():
    """ReplicaSet без owning Deployment (manual scale-down) — возвращаем сам RS."""
    pod = _mk_pod("loose-7f9c-xyz", "prod-shared",
                  owner_kind="ReplicaSet", owner_name="loose-7f9c")
    assert _pod_owner_chain_to_deployment(pod, {}) == ("prod-shared", "loose-7f9c")


def test_pod_owner_chain_no_owner_returns_none():
    """Standalone pod (kubectl run, debug) без ownerReferences."""
    pod = _mk_pod("debug-shell", "default")
    assert _pod_owner_chain_to_deployment(pod, {}) is None


def test_pod_pvc_claims_dedup_and_filter():
    pod = {
        "spec": {
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "data-0"}},
                {"name": "logs", "persistentVolumeClaim": {"claimName": "logs-0"}},
                # дубликат claim (subPath mount)
                {"name": "data2", "persistentVolumeClaim": {"claimName": "data-0"}},
                # not PVC — игнорим
                {"name": "cfg", "configMap": {"name": "x"}},
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    assert _pod_pvc_claims(pod) == ["data-0", "logs-0"]


# ── sync_pvs ────────────────────────────────────────────────────────────────


def test_sync_pvs_upserts_and_idempotent(db):
    pvs = [
        _mk_pv("pv-1", capacity="10Gi"),
        _mk_pv("pv-2", capacity="100Gi", phase="Released"),
    ]
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=pvs,
    ):
        stats1 = sync_pvs(db)
        stats2 = sync_pvs(db)

    assert stats1["pvs_fetched"] == 2
    assert stats1["pvs_upserted"] == 2
    # Second run — same count, idempotent (no duplicates).
    assert stats2["pvs_upserted"] == 2
    assert db.query(StorageVolume).filter_by(kind="pv").count() == 2

    pv2 = db.query(StorageVolume).filter_by(kind="pv", name="pv-2").one()
    assert pv2.phase == "Released"
    assert pv2.capacity_bytes == 100 * 1024**3


# ── sync_pvcs ───────────────────────────────────────────────────────────────


def test_sync_pvcs_bound_creates_bound_to_edge(db):
    # PV должен быть в KG первым.
    _upsert_volume(db, _extract_pv_fields(_mk_pv("pv-1")))
    db.commit()

    pvcs = [_mk_pvc("data-0", "prod-shared", volume_name="pv-1", phase="Bound")]
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=pvcs,
    ):
        stats = sync_pvcs(db)

    assert stats["pvcs_upserted"] == 1
    assert stats["edges_bound_to"] == 1
    edges = db.query(VolumeEdge).filter_by(kind=EDGE_BOUND_TO).all()
    assert len(edges) == 1
    e = edges[0]
    assert e.src_kind == NODE_PVC
    assert e.dst_kind == NODE_PV
    assert e.discovered_by == DISCOVERED_BY_PVC_SPEC


def test_sync_pvcs_pending_no_edge(db):
    """Pending PVC (no volume_name) → не должен создавать bound_to."""
    pvcs = [_mk_pvc("orphan", "dev-1", phase="Pending", volume_name="")]
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=pvcs,
    ):
        stats = sync_pvcs(db)
    assert stats["pvcs_upserted"] == 1
    assert stats["edges_bound_to"] == 0


def test_sync_pvcs_unknown_pv_counted(db):
    """PVC ссылается на PV которого нет в KG → skipped_unknown_pv ↑."""
    pvcs = [_mk_pvc("data-0", "prod-shared", volume_name="pv-ghost", phase="Bound")]
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=pvcs,
    ):
        stats = sync_pvcs(db)
    assert stats["pvcs_upserted"] == 1
    assert stats["skipped_unknown_pv"] == 1
    assert stats["edges_bound_to"] == 0


def test_sync_pvcs_disk_pct_attached(db):
    pvcs = [
        _mk_pvc("data-0", "prod-shared", volume_name=""),
        _mk_pvc("data-1", "prod-shared", volume_name=""),
    ]
    disk_pct_map = {("prod-shared", "data-0"): 87.5}
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=pvcs,
    ):
        stats = sync_pvcs(db, disk_pct_map=disk_pct_map)
    assert stats["disk_pct_attached"] == 1
    pvc0 = db.query(StorageVolume).filter_by(kind="pvc", name="data-0").one()
    pvc1 = db.query(StorageVolume).filter_by(kind="pvc", name="data-1").one()
    assert pvc0.disk_pct == 87.5
    assert pvc1.disk_pct is None


# ── sync_pod_pvc_edges ──────────────────────────────────────────────────────


def test_sync_pod_pvc_edges_two_pvcs_two_edges(db):
    """Pod с 2 PVC → 2 uses_volume edges от owning Service."""
    svc = upsert_service(db, namespace="prod-shared", name="clickhouse")
    _upsert_volume(db, _extract_pvc_fields(
        _mk_pvc("data-clickhouse-0", "prod-shared")))
    _upsert_volume(db, _extract_pvc_fields(
        _mk_pvc("logs-clickhouse-0", "prod-shared")))
    db.commit()

    pod = _mk_pod(
        "clickhouse-0", "prod-shared",
        pvc_claims=["data-clickhouse-0", "logs-clickhouse-0"],
        owner_kind="StatefulSet", owner_name="clickhouse",
    )
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=lambda r: [pod] if r == "pods" else [],
    ):
        stats = sync_pod_pvc_edges(db)
    assert stats["edges_uses_volume"] == 2
    edges = db.query(VolumeEdge).filter_by(
        kind=EDGE_USES_VOLUME, src_id=svc.id,
    ).all()
    assert len(edges) == 2
    for e in edges:
        assert e.src_kind == NODE_SERVICE
        assert e.dst_kind == NODE_PVC
        assert e.discovered_by == DISCOVERED_BY_PODS


def test_sync_pod_pvc_edges_standalone_pod_skipped(db):
    """Pod без ownerReferences (kubectl run) → skipped_no_owner ↑."""
    _upsert_volume(db, _extract_pvc_fields(_mk_pvc("data-0", "default")))
    db.commit()
    pod = _mk_pod("debug", "default", pvc_claims=["data-0"], owner_name="")
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=lambda r: [pod] if r == "pods" else [],
    ):
        stats = sync_pod_pvc_edges(db)
    assert stats["skipped_no_owner"] == 1
    assert stats["edges_uses_volume"] == 0


def test_sync_pod_pvc_edges_via_replicaset_resolves_deployment(db):
    """Pod → RS → Deployment chain. Service зарегистрирован под deployment name."""
    svc = upsert_service(db, namespace="prod-shared", name="auth")
    _upsert_volume(db, _extract_pvc_fields(_mk_pvc("cache-auth", "prod-shared")))
    db.commit()
    rs = _mk_rs("auth-7f9c", "prod-shared", deployment_name="auth")
    pod = _mk_pod(
        "auth-7f9c-xyz", "prod-shared",
        pvc_claims=["cache-auth"],
        owner_kind="ReplicaSet", owner_name="auth-7f9c",
    )

    def fake_kubectl(resource):
        if resource == "pods":
            return [pod]
        if resource == "replicasets":
            return [rs]
        return []

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=fake_kubectl,
    ):
        stats = sync_pod_pvc_edges(db)
    assert stats["edges_uses_volume"] == 1
    e = db.query(VolumeEdge).filter_by(kind=EDGE_USES_VOLUME).one()
    assert e.src_id == svc.id


def test_sync_pod_pvc_edges_unknown_pvc_counted(db):
    """Pod ссылается на PVC которого нет в KG → skipped_unknown_pvc ↑."""
    upsert_service(db, namespace="prod-shared", name="ch")
    db.commit()
    pod = _mk_pod(
        "ch-0", "prod-shared", pvc_claims=["ghost-pvc"],
        owner_kind="StatefulSet", owner_name="ch",
    )
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=lambda r: [pod] if r == "pods" else [],
    ):
        stats = sync_pod_pvc_edges(db)
    assert stats["skipped_unknown_pvc"] == 1
    assert stats["edges_uses_volume"] == 0


# ── end-to-end ──────────────────────────────────────────────────────────────


def test_sync_storage_full_pipeline_idempotent(db, monkeypatch):
    """sync_storage поднимает PV → PVC → pod edges. Дважды = тот же снимок."""
    upsert_service(db, namespace="prod-shared", name="clickhouse")
    db.commit()

    pvs = [_mk_pv("pv-1", capacity="100Gi")]
    pvcs = [_mk_pvc(
        "data-clickhouse-0", "prod-shared",
        volume_name="pv-1", phase="Bound",
    )]
    pod = _mk_pod(
        "clickhouse-0", "prod-shared",
        pvc_claims=["data-clickhouse-0"],
        owner_kind="StatefulSet", owner_name="clickhouse",
    )

    def fake_kubectl(resource):
        if resource == "persistentvolumes":
            return pvs
        if resource == "persistentvolumeclaims":
            return pvcs
        if resource == "pods":
            return [pod]
        return []

    # Disable disk_pct enrichment для теста (STORAGE_METRICS_ENABLED=False по default).
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=fake_kubectl,
    ):
        r1 = sync_storage(db)
        r2 = sync_storage(db)

    assert r1["pvs"]["pvs_upserted"] == 1
    assert r1["pvcs"]["edges_bound_to"] == 1
    assert r1["pod_edges"]["edges_uses_volume"] == 1
    # Идемпотентность: на втором тике те же counts (upsert) и нет дублей.
    assert r2["pvs"]["pvs_upserted"] == 1
    assert db.query(StorageVolume).filter_by(kind="pv").count() == 1
    assert db.query(StorageVolume).filter_by(kind="pvc").count() == 1
    assert db.query(VolumeEdge).filter_by(kind=EDGE_BOUND_TO).count() == 1
    assert db.query(VolumeEdge).filter_by(kind=EDGE_USES_VOLUME).count() == 1


def test_sync_storage_kubectl_failure_does_not_raise(db):
    """Если kubectl даёт пустоту — sync не падает, возвращает stats с 0."""
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=[],
    ):
        result = sync_storage(db)
    assert result["pvs"]["pvs_fetched"] == 0
    assert result["pvcs"]["pvcs_fetched"] == 0
    assert result["pod_edges"]["pods_scanned"] == 0


def test_sync_storage_disk_pct_disabled_default(db):
    """STORAGE_METRICS_ENABLED по умолчанию False — disk_pct_series=0."""
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=[],
    ):
        result = sync_storage(db)
    assert result["disk_pct_enabled"] is False
    assert result["disk_pct_series"] == 0


def test_sync_storage_disk_pct_enabled_attaches(db, monkeypatch):
    """Когда STORAGE_METRICS_ENABLED + VM URL заданы, disk_pct прилипает к PVC."""
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "STORAGE_METRICS_ENABLED", True)
    monkeypatch.setattr(app_settings, "VICTORIA_METRICS_URL", "http://vm:8428")

    pvcs = [_mk_pvc("data-0", "prod-shared", volume_name="", phase="Bound")]

    async def fake_disk_map():
        return {("prod-shared", "data-0"): 92.7}

    def fake_kubectl(resource):
        if resource == "persistentvolumeclaims":
            return pvcs
        return []

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=fake_kubectl,
    ), patch(
        "app.knowledge_graph.k8s_storage_sync._fetch_disk_pct_map",
        side_effect=fake_disk_map,
    ):
        result = sync_storage(db)

    pvc = db.query(StorageVolume).filter_by(kind="pvc", name="data-0").one()
    assert pvc.disk_pct == 92.7
    assert result["disk_pct_enabled"] is True
    assert result["disk_pct_series"] == 1


def test_upsert_volume_phase_transitions(db):
    """Изменение phase Bound→Released на повторном sync — поле обновляется."""
    f1 = _extract_pv_fields(_mk_pv("pv-1", phase="Bound"))
    _upsert_volume(db, f1)
    f2 = _extract_pv_fields(_mk_pv("pv-1", phase="Released"))
    _upsert_volume(db, f2)
    db.commit()
    pv = db.query(StorageVolume).filter_by(kind="pv", name="pv-1").one()
    assert pv.phase == "Released"


def test_upsert_volume_disk_pct_not_clobbered_by_none(db):
    """Если disk_pct=None на новом тике — старое значение сохраняется."""
    f = _extract_pvc_fields(_mk_pvc("data", "prod-shared"))
    _upsert_volume(db, f, disk_pct=75.0)
    _upsert_volume(db, f, disk_pct=None)
    db.commit()
    pvc = db.query(StorageVolume).filter_by(kind="pvc", name="data").one()
    assert pvc.disk_pct == 75.0


# ── chunking + таймаут cluster-wide листа ───────────────────────────────────
#
# `kubectl get deployments -A -o json` на этом кластере = 2318 объектов /
# ~42 МБ и в 30с из воркер-пода не влезал (см. комментарий к
# k8s_topology_resources_sync._KUBECTL_TIMEOUT_S). Листы pods/replicasets
# заведомо больше, а расплата та же: uses_volume-рёбра не создаются.


class _FakeProc:
    def __init__(self, returncode=0, stdout='{"items": []}', stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_kubectl_get_all_pages_the_list_and_waits_long_enough():
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeProc()

    with patch(
        "app.knowledge_graph.k8s_storage_sync.subprocess.run",
        side_effect=fake_run,
    ):
        assert _kubectl_get_all("pods") == []

    assert any(str(a).startswith("--chunk-size=") for a in seen["cmd"]), (
        f"лист запрашивается без пагинации: {seen['cmd']}"
    )
    assert seen["timeout"] >= 180, (
        "таймаут cluster-wide листа меньше 180с — 30с на этом кластере не "
        "хватало даже на deployments"
    )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="kubectl", timeout=180),
        None,          # rc != 0
        "bad-json",    # битый stdout
    ],
)
def test_kubectl_get_all_raises_instead_of_returning_empty(failure):
    """Отказ сигналится исключением, а НЕ пустым списком: иначе он
    неотличим от «в кластере нет объектов»."""
    if isinstance(failure, Exception):
        run = patch(
            "app.knowledge_graph.k8s_storage_sync.subprocess.run",
            side_effect=failure,
        )
    elif failure is None:
        run = patch(
            "app.knowledge_graph.k8s_storage_sync.subprocess.run",
            return_value=_FakeProc(returncode=1, stdout="", stderr="i/o timeout"),
        )
    else:
        run = patch(
            "app.knowledge_graph.k8s_storage_sync.subprocess.run",
            return_value=_FakeProc(stdout="{not json"),
        )
    with run, pytest.raises(KubectlFetchError):
        _kubectl_get_all("pods")


def test_kubectl_get_all_empty_cluster_is_not_an_error():
    """Контроль: валидный пустой лист — это НЕ отказ."""
    with patch(
        "app.knowledge_graph.k8s_storage_sync.subprocess.run",
        return_value=_FakeProc(stdout='{"items": []}'),
    ):
        assert _kubectl_get_all("pods") == []


# ── отказ fetch'а наблюдаем: errors + stats + record_source_run ─────────────


def test_pod_fetch_failure_counted_as_error_not_no_data(db):
    """Таймаут листа pod'ов → errors>0 и fetch_failed в stats + отчёт для
    decay-guard. Раньше это было `pods_scanned=0` БЕЗ ошибок, то есть отказ
    выглядел как «нет данных»."""
    def boom(resource):
        if resource == "pods":
            raise KubectlFetchError("kubectl get pods timeout=180s")
        return []

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=boom,
    ):
        stats = sync_pod_pvc_edges(db)

    assert stats["pods_scanned"] == 0
    assert stats["errors"] == 1
    assert stats["fetch_failed"] is True
    report = get_source_report(SOURCE_STORAGE_PODS)
    assert report is not None
    assert report.errors == 1        # guard увидит нездоровый источник
    assert report.fetched == 0


def test_pvc_fetch_failure_counted_as_error(db):
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=KubectlFetchError("rc=1"),
    ):
        stats = sync_pvcs(db)

    assert stats["errors"] == 1
    assert stats["fetch_failed"] is True
    report = get_source_report(SOURCE_STORAGE_PVCS)
    assert report is not None and report.errors == 1


def test_sync_storage_sums_fetch_errors_into_top_level_stats(db):
    """Все 4 листа (pv/pvc/pods/rs) упали → это видно в результате таска."""
    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=KubectlFetchError("timeout"),
    ):
        result = sync_storage(db)

    assert result["errors"] == 4
    assert result["pvs"]["fetch_failed"] is True
    assert result["pvcs"]["fetch_failed"] is True
    assert result["pod_edges"]["fetch_failed"] is True


# ── decay volume-рёбер ──────────────────────────────────────────────────────


def _seed_volume_edges(db, count=4, stale=(), age_hours=72):
    """Service + `count` PVC + uses_volume рёбра. Индексы из `stale` получают
    искусственный возраст (не подтверждены синком)."""
    svc = upsert_service(db, namespace="prod-shared", name="clickhouse")
    db.flush()
    edges = []
    for i in range(count):
        vol = _upsert_volume(
            db, _extract_pvc_fields(_mk_pvc(f"data-{i}", "prod-shared")),
        )
        edges.append(_upsert_volume_edge(
            db,
            src_kind=NODE_SERVICE, src_id=svc.id,
            dst_kind=NODE_PVC, dst_id=vol.id,
            kind=EDGE_USES_VOLUME, discovered_by=DISCOVERED_BY_PODS,
        ))
    for i in stale:
        edges[i].last_seen_at = datetime.utcnow() - timedelta(hours=age_hours)
    db.commit()
    return svc, edges


def _edge_ids(db):
    return {e.id for e in db.query(VolumeEdge).all()}


def test_volume_edges_decay_when_source_healthy(db):
    """Удалённый pod → его uses_volume-ребро не подтверждается и уходит."""
    _svc, edges = _seed_volume_edges(db, count=4, stale=(0,))
    doomed = edges[0].id
    record_source_run(
        SOURCE_STORAGE_PODS, {"pods_scanned": 120, "errors": 0},
    )

    stats = decay_volume_edges(db)
    db.commit()

    assert stats["deleted"] == 1
    assert stats["blocked_kinds"] == {}
    assert doomed not in _edge_ids(db)
    assert db.query(VolumeEdge).count() == 3


def test_volume_edges_not_decayed_when_source_reported_errors(db):
    """Срез pod'ов сбойнул (errors>0) → его рёбра не трогаем: их возраст —
    артефакт сбоя, а не убыль storage."""
    _svc, edges = _seed_volume_edges(db, count=4, stale=(0,))
    record_source_run(
        SOURCE_STORAGE_PODS, {"pods_scanned": 0, "errors": 1},
    )

    stats = decay_volume_edges(db)

    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {REASON_FETCH_ERRORS: {EDGE_USES_VOLUME: 1}}
    assert edges[0].id in _edge_ids(db)
    assert SOURCE_STORAGE_PODS in stats["stale_sources"]


def test_volume_edges_not_decayed_on_suspicious_zero_fetch(db):
    """`pods_scanned=0` без errors (таймаут в прошлой реализации выглядел
    именно так) — тоже блокирует decay."""
    _svc, edges = _seed_volume_edges(db, count=4, stale=(0,))
    record_source_run(SOURCE_STORAGE_PODS, {"pods_scanned": 0, "errors": 0})

    stats = decay_volume_edges(db)

    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {REASON_EMPTY_FETCH: {EDGE_USES_VOLUME: 1}}
    assert edges[0].id in _edge_ids(db)


def test_volume_edges_not_decayed_when_source_is_silent(db):
    """Отчёта нет вовсе (синк в другом процессе) и ни одно ребро источника не
    освежалось за окно → фоллбэк по данным блокирует чистку."""
    _svc, edges = _seed_volume_edges(db, count=4, stale=(0, 1, 2, 3))

    stats = decay_volume_edges(db)

    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {
        REASON_NO_RECENT_REFRESH: {EDGE_USES_VOLUME: 4},
    }
    assert len(_edge_ids(db)) == 4


def test_volume_decay_respects_delete_pct_cap(db):
    """Массовая «пропажа» рёбер (>25%) — симптом сбоя, весь проход отменяется."""
    _svc, _edges = _seed_volume_edges(db, count=4, stale=(0, 1))
    record_source_run(SOURCE_STORAGE_PODS, {"pods_scanned": 120, "errors": 0})

    stats = decay_volume_edges(db)

    assert stats["skipped_decay"] == 1
    assert stats["deleted"] == 0
    assert db.query(VolumeEdge).count() == 4


def test_volume_decay_keeps_fresh_edges(db):
    """Контроль: свежие рёбра здорового источника не трогаются вовсе."""
    _seed_volume_edges(db, count=4)
    record_source_run(SOURCE_STORAGE_PODS, {"pods_scanned": 120, "errors": 0})

    stats = decay_volume_edges(db)

    assert stats["deleted"] == 0
    assert stats["blocked_kinds"] == {}
    assert db.query(VolumeEdge).count() == 4


# ── чистка узлов kg_storage_volumes по снимку кластера ──────────────────────


def _pvc_names(db):
    return {
        v.name for v in db.query(StorageVolume).filter_by(kind="pvc").all()
    }


def test_absent_pvc_is_removed_with_its_edges(db):
    """PVC удалили из кластера → узел и его рёбра уходят из графа, иначе он
    вечно тянется в blast-radius."""
    _svc, edges = _seed_volume_edges(db, count=4)
    gone_edge = edges[3].id
    snapshot = [_mk_pvc(f"data-{i}", "prod-shared") for i in range(3)]

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=snapshot,
    ):
        stats = sync_pvcs(db)

    assert stats["cleanup"]["volumes_deleted"] == 1
    assert stats["cleanup"]["edges_deleted"] == 1
    assert _pvc_names(db) == {"data-0", "data-1", "data-2"}
    assert gone_edge not in _edge_ids(db)


def test_absent_pvc_kept_when_fetch_failed(db):
    """Сбой fetch'а → снимок неполный, чистка пропущена, данные целы."""
    _seed_volume_edges(db, count=4)

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=KubectlFetchError("timeout"),
    ):
        stats = sync_pvcs(db)

    assert stats["errors"] == 1
    assert stats["cleanup"]["skipped"] == "fetch_failed"
    assert len(_pvc_names(db)) == 4
    assert len(_edge_ids(db)) == 4


def test_absent_pvc_kept_on_empty_snapshot(db):
    """Пустой лист неотличим от пустого кластера → не чистим (дисциплина
    cleanup_stale_jobs)."""
    _seed_volume_edges(db, count=4)

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=[],
    ):
        stats = sync_pvcs(db)

    assert stats["cleanup"]["skipped"] == "empty_fetch"
    assert len(_pvc_names(db)) == 4


def test_volume_cleanup_respects_delete_pct_cap(db):
    """Пропало 3 из 4 узлов (75%) → чистка отменяется целиком."""
    _seed_volume_edges(db, count=4)

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        return_value=[_mk_pvc("data-0", "prod-shared")],
    ):
        stats = sync_pvcs(db)

    assert stats["cleanup"]["skipped"] == "delete_pct"
    assert len(_pvc_names(db)) == 4


def test_sync_storage_drops_deleted_pvc_end_to_end(db):
    """Сквозной тик: PVC удалён в кластере → его узел и uses_volume-ребро
    исчезают из графа на следующем прогоне."""
    upsert_service(db, namespace="prod-shared", name="clickhouse")
    db.commit()

    def snapshot(pvc_count):
        pvcs = [
            _mk_pvc(f"data-{i}", "prod-shared") for i in range(pvc_count)
        ]
        pod = _mk_pod(
            "clickhouse-0", "prod-shared",
            pvc_claims=[f"data-{i}" for i in range(pvc_count)],
            owner_kind="StatefulSet", owner_name="clickhouse",
        )

        def fake(resource):
            if resource == "persistentvolumeclaims":
                return pvcs
            if resource == "pods":
                return [pod]
            return []
        return fake

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=snapshot(4),
    ):
        first = sync_storage(db)
    assert first["pod_edges"]["edges_uses_volume"] == 4
    assert db.query(VolumeEdge).filter_by(kind=EDGE_USES_VOLUME).count() == 4

    with patch(
        "app.knowledge_graph.k8s_storage_sync._kubectl_get_all",
        side_effect=snapshot(3),
    ):
        second = sync_storage(db)

    assert second["errors"] == 0
    assert _pvc_names(db) == {"data-0", "data-1", "data-2"}
    assert db.query(VolumeEdge).filter_by(kind=EDGE_USES_VOLUME).count() == 3
