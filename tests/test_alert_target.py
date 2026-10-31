"""Цепочка атрибуции алерта (alert_target): объект, а не экспортер метрики.

Метки в тестах — с живых алертов недели 24–31.10.2026 (ALERTS в VM),
обезличенные до нужных ключей.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.timeutil import utcnow
from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.schema import (AlertEvent, Service, StorageVolume,
                                        VolumeEdge)
from app.models.incident import Incident
from app.scripts import reattribute_alerts as ra
from app.services import alert_target as at
from app.services.alert_target import resolve_alert_target

KSM = {"job": "kube-state-metrics", "service": "vm-kube-state-metrics",
       "pod": "vm-kube-state-metrics-59b86ff96-qwhmz", "container": "kube-state-metrics",
       "instance": "192.168.2.45:8080"}
NODE_EXP = {"job": "node-exporter", "service": "vm-node-exporter",
            "pod": "vm-node-exporter-7hnfd", "container": "node-exporter",
            "namespace": "monitoring", "instance": "192.168.149.84:9100"}


class FakeGraph:
    """Граф из словарей — как в dry-run переатрибуции на выгрузке."""

    def __init__(self, workloads=(), pvcs=None, jobs=None):
        self.workloads = set(workloads)
        self.pvcs = pvcs or {}
        self.jobs = jobs or {}

    def workload_exists(self, namespace, name):
        return (namespace, name) in self.workloads

    def pvc_owner(self, namespace, pvc):
        return self.pvcs.get((namespace, pvc))

    def job_target(self, namespace, job_name):
        return self.jobs.get((namespace, job_name), (job_name, "job_name"))


def _t(labels, graph=None, legacy=None):
    return resolve_alert_target(labels, legacy_default=legacy, lookup=graph or FakeGraph())


# ── Ветки цепочки ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["deployment", "statefulset", "daemonset"])
def test_workload_label_wins_over_ksm(key):
    t = _t({**KSM, "alertname": "X", "namespace": "prod-kingdom5", key: "nats"},
           legacy="vm-kube-state-metrics")
    assert (t.service, t.scope, t.how, t.source) == (
        "nats", at.TARGET_SERVICE, "workload_label", "vm-kube-state-metrics")


def test_cronjob_label():
    t = _t({**KSM, "alertname": "KubeCronJobFailed", "namespace": "mcp", "cronjob": "wiki-sync"})
    assert (t.service, t.how) == ("wiki-sync", "cronjob")


def test_job_name_goes_through_job_attribution():
    graph = FakeGraph(jobs={("mcp", "wiki-sync-29779065"): ("mcp-wiki", "job_owner")})
    t = _t({**KSM, "alertname": "KubeJobFailed", "namespace": "mcp",
            "job_name": "wiki-sync-29779065"}, graph)
    assert (t.service, t.how) == ("mcp-wiki", "job")


@pytest.mark.parametrize("key", ["horizontalpodautoscaler", "poddisruptionbudget"])
def test_hpa_pdb_name_is_target(key):
    t = _t({**KSM, "alertname": "KubePdbNotEnoughHealthyPods",
            "namespace": "squad-28-kingdom2", key: "town-grainhost"})
    assert (t.service, t.scope) == ("town-grainhost", at.TARGET_SERVICE)


def test_pvc_resolves_to_mounting_workload():
    graph = FakeGraph(pvcs={("prod-kingdom7", "data-town-db-0"): "town-db"})
    t = _t({**KSM, "alertname": "KubePersistentVolumeFillingUp",
            "namespace": "prod-kingdom7", "persistentvolumeclaim": "data-town-db-0"}, graph)
    assert (t.service, t.how) == ("town-db", "pvc")


def test_pvc_unknown_falls_through_chain():
    t = _t({**KSM, "alertname": "KubePersistentVolumeFillingUp",
            "namespace": "prod-kingdom7", "persistentvolumeclaim": "gone"})
    assert t.scope == at.TARGET_UNRESOLVED and t.service is None


def test_pod_via_exporter_needs_workload_in_graph():
    labels = {**KSM, "alertname": "KubePodCrashLooping", "namespace": "squad-49-kingdom2",
              "pod": "bot-service-64f9bdf-th89b", "container": "bot-service"}
    hit = _t(labels, FakeGraph(workloads={("squad-49-kingdom2", "bot-service")}))
    assert (hit.service, hit.how) == ("bot-service", "pod")
    miss = _t(labels)
    assert miss.service is None and miss.scope == at.TARGET_UNRESOLVED


def test_cadvisor_pod_without_exporter_trusts_pod_name():
    """CPUThrottlingHigh: ни service, ни job — только pod/container/namespace.
    Замер 31.10.2026: ~110 таких алертов за неделю лежали без сервиса."""
    t = _t({"alertname": "CPUThrottlingHigh", "namespace": "squad-17-shared",
            "pod": "config-db-postgresql-0", "container": "metrics", "instance": "dev-3"})
    assert (t.service, t.scope, t.how, t.source) == (
        "config-db-postgresql", at.TARGET_SERVICE, "pod", None)


def test_container_with_namespace_from_graph():
    labels = {**KSM, "alertname": "KubeContainerWaiting", "namespace": "sre-ai",
              "pod": "", "container": "worker"}
    assert _t(labels, FakeGraph(workloads={("sre-ai", "worker")})).how == "container"
    assert _t(labels).scope == at.TARGET_UNRESOLVED


def test_scrape_job_names_object_and_its_namespace():
    t = _t({"alertname": "ScrapePoolHasNoTargets", "namespace": "monitoring",
            "job": "vmagent-vm-victoria-metrics-k8s-stack",
            "service": "vmagent-vm-victoria-metrics-k8s-stack", "container": "vmagent",
            "scrape_job": "serviceScrape/prod-shared/chat-db-postgresql/0"},
           legacy="vmagent-vm-victoria-metrics-k8s-stack")
    assert (t.service, t.namespace, t.how, t.source) == (
        "chat-db-postgresql", "prod-shared", "scrape_job",
        "vmagent-vm-victoria-metrics-k8s-stack")


def test_target_down_ns_job():
    t = _t({"alertname": "TargetDown", "namespace": "squad-55-kingdom2",
            "job": "squad-55-kingdom2/town-grainhost"})
    assert (t.service, t.namespace, t.how) == ("town-grainhost", "squad-55-kingdom2", "job_label")


def test_service_label_when_not_exporter():
    t = _t({"alertname": "X", "job": "kube-state-metrics", "service": "auth-service",
            "namespace": "prod-shared"})
    assert (t.service, t.how) == ("auth-service", "service_label")


def test_app_alert_keeps_legacy_default():
    t = _t({"alertname": "HighLatency", "service": "town-service", "namespace": "s"},
           legacy="town-service")
    assert (t.service, t.scope) == ("town-service", at.TARGET_LEGACY)


# ── Нода ─────────────────────────────────────────────────────────────────────

def test_node_exporter_alert_is_node_not_service():
    t = _t({**NODE_EXP, "alertname": "NodeDiskIOSaturation", "node": "dev-16"},
           legacy="vm-node-exporter")
    assert (t.service, t.scope, t.node) == (None, at.TARGET_NODE, "dev-16")


def test_kube_node_alert_via_ksm_is_node():
    t = _t({**KSM, "pod": "", "alertname": "KubeNodePressure", "node": "dev-32",
            "namespace": "monitoring"})
    assert t.scope == at.TARGET_NODE and t.node == "dev-32"


def test_node_without_pod_is_node():
    t = _t({**KSM, "pod": "", "container": "", "alertname": "SomethingOnNode", "node": "dev-4"})
    assert t.scope == at.TARGET_NODE


def test_pod_on_node_is_pod_target_not_node():
    """DevContainerMemoryWillHitLimit: node + pod — объект под, нода в extras."""
    t = _t({"alertname": "DevContainerMemoryWillHitLimit", "namespace": "squad-11-kingdom2",
            "pod": "seq-857cff8f7c-gctp4", "container": "seq", "node": "dev-1"})
    assert (t.service, t.node) == ("seq", "dev-1")


# ── Namespace ────────────────────────────────────────────────────────────────

def test_namespace_only_alert_is_namespace_scope():
    t = _t({"alertname": "ProdRestartsSpike", "namespace": "prod-kingdom1"})
    assert (t.service, t.scope) == (None, at.TARGET_NAMESPACE)


def test_exporter_namespace_is_not_the_object():
    """У алерта через экспортер `namespace` — ns экспортера (monitoring)."""
    t = _t({"alertname": "TooManyLogs", "namespace": "monitoring",
            "job": "vmagent-vm-victoria-metrics-k8s-stack"})
    assert t.scope == at.TARGET_UNRESOLVED


# ── Экспортер — цель только для алерта про сам экспортер ─────────────────────

@pytest.mark.parametrize("labels,name", [
    ({"alertname": "TargetDown", "job": "node-exporter", "namespace": "monitoring"},
     "node-exporter"),
    ({**NODE_EXP, "alertname": "ProdNodeExporterDown", "node": "k8s-master-3"},
     "vm-node-exporter"),
    ({"alertname": "PostgresExporterDown", "job": "prod-shared/postgres-exporter",
      "namespace": "prod-shared"}, "postgres-exporter"),
])
def test_exporter_self_alert_targets_exporter(labels, name):
    t = _t(labels)
    assert (t.service, t.scope, t.how) == (name, at.TARGET_EXPORTER, "exporter_self")


def test_exporter_pod_crashloop_is_about_exporter():
    """Под nats-exporter в CrashLoop — алерт про сам экспортер."""
    labels = {**KSM, "alertname": "KubePodCrashLooping", "namespace": "squad-11-kingdom2",
              "pod": "nats-exporter-prometheus-nats-exporter-77bc49f6b4-zxvxq",
              "container": "prometheus-nats-exporter"}
    graph = FakeGraph(workloads={("squad-11-kingdom2", "nats-exporter-prometheus-nats-exporter")})
    t = _t(labels, graph)
    assert (t.service, t.scope) == ("nats-exporter-prometheus-nats-exporter", at.TARGET_EXPORTER)


def test_ksm_own_pod_never_target():
    t = _t({**KSM, "alertname": "KubePodNotReady", "namespace": "monitoring"},
           FakeGraph(workloads={("monitoring", "vm-kube-state-metrics")}))
    assert t.service is None and t.scope == at.TARGET_UNRESOLVED


@pytest.mark.parametrize("name,expected", [
    ("vm-kube-state-metrics", True), ("vmagent-vm-victoria-metrics-k8s-stack", True),
    ("postgres-exporter", True), ("nats-exporter-prometheus-nats-exporter", True),
    ("town-db-cnpg-metrics", False), ("town-service", False), ("", False), (None, False),
])
def test_is_exporter_name(name, expected):
    assert at.is_exporter_name(name) is expected


def test_kill_switch_returns_legacy(monkeypatch):
    monkeypatch.setattr(at.settings, "ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED", False)
    t = _t({"alertname": "CPUThrottlingHigh", "namespace": "s", "pod": "a-0"}, legacy="x")
    assert (t.service, t.scope) == ("x", at.TARGET_LEGACY)


# ── Класс строки для сверки ──────────────────────────────────────────────────

@pytest.mark.parametrize("args,kw,expected", [
    ((None, None, {"target_kind": "node"}), {}, at.ATTR_NODE),
    ((None, None, {"attribution": "node", "source": "vm-node-exporter"}), {}, at.ATTR_NODE),
    ((None, None, {"target_kind": "namespace"}), {}, at.ATTR_NAMESPACE),
    ((None, None, {"target_kind": "unresolved"}), {}, at.ATTR_NO_SERVICE),
    ((None, None, None), {}, at.ATTR_NO_SERVICE),
    (("vm-node-exporter", False, {"target_kind": "exporter"}), {}, at.ATTR_EXPORTER_SELF),
    (("vm-kube-state-metrics", False, {}), {}, at.ATTR_ON_EXPORTER),
    (("map-db-postgresql", True, {}), {}, at.ATTR_ON_EXPORTER),
    (("map-db-postgresql", True, {}), {"real_workload": True}, at.ATTR_SERVICE),
    (("town-service", False, {}), {}, at.ATTR_SERVICE),
])
def test_classify_alert_row(args, kw, expected):
    assert at.classify_alert_row(*args, **kw) == expected


# ── Store-путь: kg_alerts ────────────────────────────────────────────────────

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _incident(fp, labels, namespace):
    return Incident(
        incident_id=fp, severity="warning", status="firing", summary="x",
        description="y", namespace=namespace, labels={"severity": "warning", **labels},
        annotations={}, starts_at="2026-10-30T10:00:00Z",
    )


def test_store_cadvisor_alert_lands_on_workload(db):
    populate_from_incident(db, _incident("fp-cpu", {
        "alertname": "CPUThrottlingHigh", "namespace": "squad-17-shared",
        "pod": "config-db-postgresql-0", "container": "metrics",
    }, "squad-17-shared"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-cpu").one()
    svc = db.get(Service, alert.service_id)
    assert (svc.namespace, svc.name) == ("squad-17-shared", "config-db-postgresql")
    assert alert.raw["target_kind"] == "service" and alert.raw["attribution"] == "pod"


def test_store_scrape_pool_uses_scrape_namespace(db):
    populate_from_incident(db, _incident("fp-scrape", {
        "alertname": "ScrapePoolHasNoTargets", "namespace": "monitoring",
        "job": "vmagent-vm-victoria-metrics-k8s-stack",
        "service": "vmagent-vm-victoria-metrics-k8s-stack",
        "scrape_job": "serviceScrape/prod-shared/chat-db-postgresql/0",
    }, "monitoring"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-scrape").one()
    svc = db.get(Service, alert.service_id)
    assert (svc.namespace, svc.name) == ("prod-shared", "chat-db-postgresql")


def test_store_namespace_alert_marked(db):
    populate_from_incident(db, _incident("fp-ns", {
        "alertname": "ProdRestartsSpike", "namespace": "prod-kingdom1",
    }, "prod-kingdom1"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-ns").one()
    assert alert.service_id is None
    assert alert.raw["target_kind"] == "namespace" and alert.raw["namespace"] == "prod-kingdom1"


def test_store_node_alert_marked(db):
    populate_from_incident(db, _incident("fp-node", {
        **NODE_EXP, "alertname": "NodeMemoryWillExhaustSoon", "node": "dev-23",
    }, "monitoring"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-node").one()
    assert alert.service_id is None
    assert alert.raw["target_kind"] == "node" and alert.raw["node"] == "dev-23"


def test_db_lookup_pvc_owner(db):
    svc = Service(namespace="prod-kingdom7", name="town-db", node_kind="workload")
    vol = StorageVolume(kind="pvc", namespace="prod-kingdom7", name="data-town-db-0")
    db.add_all([svc, vol])
    db.flush()
    db.add(VolumeEdge(src_kind="service", src_id=svc.id, dst_kind="pvc", dst_id=vol.id,
                      kind="uses_volume"))
    db.flush()
    graph = at.DbLookup(db)
    assert graph.pvc_owner("prod-kingdom7", "data-town-db-0") == "town-db"
    assert graph.pvc_owner("prod-kingdom7", "nope") is None
    assert at.DbLookup(None).pvc_owner("prod-kingdom7", "data-town-db-0") is None


# ── Скрипт переатрибуции ─────────────────────────────────────────────────────

def test_am_fingerprint_matches_alertmanager():
    # Эталон — fingerprint живого алерта (kg_alerts.fingerprint) и его метки.
    assert ra.am_fingerprint({}) == "cbf29ce484222325"
    a = ra.am_fingerprint({"alertname": "X", "namespace": "n"})
    assert a == ra.am_fingerprint({"namespace": "n", "alertname": "X"})
    assert a != ra.am_fingerprint({"alertname": "X", "namespace": "m"})


def test_labels_by_fingerprint_drops_series_only_labels():
    series = [{"__name__": "ALERTS", "alertstate": "firing", "alertname": "X"}]
    assert ra.labels_by_fingerprint(series) == {ra.am_fingerprint({"alertname": "X"}): {"alertname": "X"}}


def _row(db, fp, alertname, svc=None, raw=None):
    now = utcnow().replace(tzinfo=None)
    db.add(AlertEvent(service_id=svc.id if svc else None, alertname=alertname,
                      severity="warning", fingerprint=fp, fired_at=now - timedelta(days=1),
                      raw=raw or {"description": "d"}))
    db.flush()


def test_reattribute_dry_run_and_apply(db):
    ksm = Service(namespace="monitoring", name="vm-kube-state-metrics")
    town = Service(namespace="prod-k5", name="town-service")
    db.add_all([ksm, town])
    db.flush()
    cpu = {"alertname": "CPUThrottlingHigh", "namespace": "squad-17-shared",
           "pod": "config-db-postgresql-0", "container": "metrics"}
    ns_only = {"alertname": "ProdRestartsSpike", "namespace": "prod-kingdom1"}
    ok = {"alertname": "HighLatency", "service": "town-service", "namespace": "prod-k5"}
    nolabels = {"alertname": "Gone"}
    fp = {name: ra.am_fingerprint(lbl) for name, lbl in
          (("cpu", cpu), ("ns", ns_only), ("ok", ok), ("gone", nolabels))}
    _row(db, fp["cpu"], "CPUThrottlingHigh")
    _row(db, fp["ns"], "ProdRestartsSpike", ksm)
    _row(db, fp["ok"], "HighLatency", town)
    _row(db, fp["gone"], "Gone")
    db.commit()
    series = [{"__name__": "ALERTS", "alertstate": "firing", **lbl} for lbl in (cpu, ns_only, ok)]

    dry = ra.reattribute(db, series, days=7)
    assert dry["total"] == 4 and dry["no_labels"] == 1
    assert dry["before"] == {"no_service": 2, "on_exporter": 1, "service": 1}
    assert dry["after"] == {"service": 2, "namespace": 1, "no_service": 1}
    assert db.query(AlertEvent).filter_by(fingerprint=fp["cpu"]).one().service_id is None

    ra.reattribute(db, series, days=7, apply=True)
    moved = db.query(AlertEvent).filter_by(fingerprint=fp["cpu"]).one()
    assert db.get(Service, moved.service_id).name == "config-db-postgresql"
    assert moved.raw["reattributed_from"]["service_id"] is None
    ns_row = db.query(AlertEvent).filter_by(fingerprint=fp["ns"]).one()
    assert ns_row.service_id is None and ns_row.raw["target_kind"] == "namespace"
    assert ns_row.raw["reattributed_from"]["service_id"] == ksm.id
    assert db.query(AlertEvent).filter_by(fingerprint=fp["ok"]).one().service_id == town.id
    # Идемпотентно: второй прогон ничего не переносит.
    again = ra.reattribute(db, series, days=7, apply=True)
    assert again["moved"] == 0


def test_reattribute_main_reads_series_file(tmp_path, db, monkeypatch, capsys):
    path = tmp_path / "series.json"
    path.write_text('{"status": "success", "data": []}', encoding="utf-8")
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    assert ra.main(["--series-file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "было:" in out and "стало:" in out and "dry-run" in out


def test_reattribute_ignores_rows_outside_window(db):
    db.add(AlertEvent(alertname="Old", severity="warning", fingerprint="old",
                      fired_at=datetime(2020, 1, 1)))
    db.commit()
    assert ra.reattribute(db, [], days=7)["total"] == 0


def test_short_rs_hash_only_in_k8s_alphabet():
    assert at.strip_pod_hash("bot-service-64f9bdf-th89b") == "bot-service"
    # Под Job-а: «migrate» не rs-hash (гласные) — без ложного deployment.
    assert at.strip_pod_hash("town-db-migrate-x9k2p") is None


def test_am_fingerprint_live_sample():
    """Fingerprint живого алерта недели 31.10.2026 и метки его серии ALERTS."""
    labels = {"namespace": "prod-kingdom1", "alertgroup": "prod-availability",
              "alertname": "ProdRestartsSpike", "severity": "critical"}
    assert ra.am_fingerprint(labels) == "3e41457cb89b8f8e"


def test_reattribute_marks_exporter_self_without_moving(db):
    """CrashLoop пода nats-exporter уже лежит на nats-exporter: сервис и
    инцидент не трогаем, только пометка «алерт про сам экспортер»."""
    nats_exp = Service(namespace="squad-11-kingdom2", name="nats-exporter-prometheus-nats-exporter")
    db.add(nats_exp)
    db.flush()
    db.add(Service(namespace="squad-11-kingdom2", name="nats-exporter-prometheus-nats-exporter",
                   node_kind="workload"))
    labels = {**KSM, "alertname": "KubePodCrashLooping", "namespace": "squad-11-kingdom2",
              "pod": "nats-exporter-prometheus-nats-exporter-77bc49f6b4-zxvxq",
              "container": "prometheus-nats-exporter"}
    fp = ra.am_fingerprint(labels)
    _row(db, fp, "KubePodCrashLooping", nats_exp)
    db.query(AlertEvent).filter_by(fingerprint=fp).one().incident_id = "inc-1"
    db.commit()
    res = ra.reattribute(db, [labels], days=7, apply=True)
    assert res["moved"] == 1 and res["after"] == {"exporter_self": 1}
    row = db.query(AlertEvent).filter_by(fingerprint=fp).one()
    assert row.service_id == nats_exp.id and row.incident_id == "inc-1"
    assert row.raw["target_kind"] == "exporter"
