"""Вид и имя рабочей нагрузки цели алерта (app/remediation/workload.py).

Имена и лейблы — как у живых алертов: у `CPUThrottlingHigh` есть только
`pod`/`container`, вид приходится выводить из графа или из формы имени пода.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.remediation.workload import (KIND_UNKNOWN, WorkloadTarget,
                                      resolve_workload_target)

NS = "squad-7-k5"


@pytest.fixture
def db():
    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


def _workload(db, name: str, kind: str, namespace: str = NS) -> None:
    from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service
    db.add(Service(name=name, namespace=namespace, node_kind=NODE_KIND_WORKLOAD,
                   metadata_json={"k8s_workload": {"workload_kind": kind, "replicas": 1}}))
    db.commit()


def _service(db, name: str, namespace: str = NS) -> None:
    from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service
    db.add(Service(name=name, namespace=namespace, node_kind=NODE_KIND_SERVICE,
                   metadata_json={"k8s_service": {"service_type": "ClusterIP"}}))
    db.commit()


def _cpu(pod: str, **extra) -> dict:
    return {"alertname": "CPUThrottlingHigh", "namespace": NS, "pod": pod,
            "container": "main", **extra}


# ── явные лейблы ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,kind", [
    ("deployment", "Deployment"), ("statefulset", "StatefulSet"),
    ("daemonset", "DaemonSet"), ("job_name", "Job"), ("cronjob", "CronJob"),
])
def test_explicit_label_wins_without_db(label, kind):
    got = resolve_workload_target({"namespace": NS, label: "thing", "pod": "ksm-0"})
    assert got == WorkloadTarget(kind=kind, name="thing", source="alert_label")


# ── граф ──────────────────────────────────────────────────────────────────

def test_statefulset_pod_resolved_from_graph(db):
    _workload(db, "town-db-postgresql", "StatefulSet")
    _service(db, "town-db-postgresql")          # k8s Service с тем же именем — не вид
    got = resolve_workload_target(_cpu("town-db-postgresql-0"), db=db)
    assert got == WorkloadTarget("StatefulSet", "town-db-postgresql", "kg_workload")


def test_deployment_pod_resolved_from_graph(db):
    _workload(db, "message-db-backup", "Deployment")
    got = resolve_workload_target(_cpu("message-db-backup-cc4696c69-dzzqd"), db=db)
    assert got == WorkloadTarget("Deployment", "message-db-backup", "kg_workload")


def test_cnpg_cluster_pod_resolved_from_graph(db):
    _workload(db, "push-db-cnpg", "Cluster")
    got = resolve_workload_target(_cpu("push-db-cnpg-1"), db=db)
    assert got.kind == "Cluster" and got.name == "push-db-cnpg"


def test_daemonset_with_hashlike_word_is_not_a_deployment(db):
    """`<ds>-prometheus-<hash>`: «prometheus» — 10 знаков и похоже на rs-hash."""
    _workload(db, "node-exporter-prometheus", "DaemonSet")
    got = resolve_workload_target(_cpu("node-exporter-prometheus-x7k2p"), db=db)
    assert got == WorkloadTarget("DaemonSet", "node-exporter-prometheus", "kg_workload")


def test_same_named_deployment_does_not_capture_statefulset_pod(db):
    """В графе под именем `foo` записан Deployment, а алерт — с пода StatefulSet `foo-0`."""
    _workload(db, "foo", "Deployment")
    t = resolve_workload_target({"pod": "foo-0"}, namespace=NS, db=db)
    assert (t.kind, t.name, t.source) == (None, None, "ambiguous")


def test_same_named_statefulset_does_not_capture_deployment_pod(db):
    _workload(db, "foo", "StatefulSet")
    t = resolve_workload_target({"pod": "foo-7d9f8c6b5-x2k4q"}, namespace=NS, db=db)
    assert (t.kind, t.name, t.source) == (None, None, "ambiguous")


def test_other_namespace_is_not_consulted(db):
    _workload(db, "town-db-postgresql", "Deployment", namespace="squad-9-k1")
    got = resolve_workload_target(_cpu("town-db-postgresql-0"), db=db)
    assert got.kind == "StatefulSet" and got.source == "pod_name"


def test_job_pod_resolved_from_kg_jobs(db):
    from app.knowledge_graph.schema import K8sJob
    db.add(K8sJob(namespace=NS, name="town-db-migrate", kind="job"))
    db.commit()
    got = resolve_workload_target(_cpu("town-db-migrate-x9k2p"), db=db)
    assert got == WorkloadTarget("Job", "town-db-migrate", "kg_job")


def test_service_name_used_only_without_pod(db):
    _workload(db, "town-service", "Deployment")
    got = resolve_workload_target({"namespace": NS}, service="town-service", db=db)
    assert got == WorkloadTarget("Deployment", "town-service", "kg_workload")
    unknown = resolve_workload_target({"namespace": NS}, service="ghost", db=db)
    assert unknown.kind is None and unknown.name == "ghost"
    assert unknown.kind_label == KIND_UNKNOWN


# ── форма имени пода (граф объекта не знает) ──────────────────────────────

@pytest.mark.parametrize("pod,kind,name", [
    # Deployment без k8s Service: workload-узла в графе нет.
    ("copilot-worker-8596b5496-fnrxt", "Deployment", "copilot-worker"),
    ("bot-service-64f9bdf-th89b", "Deployment", "bot-service"),
    ("town-db-postgresql-0", "StatefulSet", "town-db-postgresql"),
    ("clickhouse-shard0-12", "StatefulSet", "clickhouse-shard0"),
    # Job от CronJob: хвост — время запуска, не rs-hash.
    ("gitlab-backup-29916180-7xk2p", "Job", "gitlab-backup-29916180"),
    # «prometheus» в хвосте — не алфавит SafeEncodeString: не Deployment.
    ("node-exporter-prometheus-x7k2p", None, None),
    ("standalone", None, None),
])
def test_pod_name_shape_fallback(db, pod, kind, name):
    for session in (None, db):
        got = resolve_workload_target(_cpu(pod), db=session)
        assert (got.kind, got.name) == (kind, name)
        assert got.source == ("pod_name" if kind else "none")


def test_broken_session_degrades_to_shape():
    class _Boom:
        def begin_nested(self):
            raise RuntimeError("db down")

    got = resolve_workload_target(_cpu("town-db-postgresql-0"), db=_Boom())
    assert got.kind == "StatefulSet" and got.source == "pod_name"


def test_metadata_without_kind_is_ignored(db):
    from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service
    db.add(Service(name="town-service", namespace=NS, node_kind=NODE_KIND_WORKLOAD,
                   metadata_json={"k8s_workload": {"workload_kind": "Rollout"}}))
    db.commit()
    got = resolve_workload_target(_cpu("town-service-6c6cd4df-8hx9c"), db=db)
    assert got == WorkloadTarget("Deployment", "town-service", "pod_name")
