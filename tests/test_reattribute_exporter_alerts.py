"""Перенос истории алертов с экспортеров на k8s-target (reattribute_exporter_alerts)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.incidents import attach_alert
from app.knowledge_graph.populator import record_alert_event, upsert_service
from app.knowledge_graph.schema import AlertEvent, KGIncident, Service
from app.scripts.reattribute_exporter_alerts import reattribute, resolve_target, undo


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


T0 = datetime(2026, 10, 1, 10, 0)


def _alert_on(db, exporter_ns, exporter, fp, alertname, description, resolved=True):
    svc = upsert_service(db, namespace=exporter_ns, name=exporter)
    db.flush()
    record_alert_event(
        db, service=svc, alertname=alertname, severity="warning",
        fingerprint=fp, fired_at=T0, incident_id=fp, raw={"description": description},
    )
    attach_alert(db, namespace=exporter_ns, service_name=exporter, service_id=svc.id,
                 fired_at=T0, alertname=alertname, severity="warning", fingerprint=fp)
    if resolved:
        db.query(AlertEvent).filter_by(fingerprint=fp).one().resolved_at = T0 + timedelta(minutes=30)
    db.commit()


def _seed(db):
    upsert_service(db, namespace="squad-16-shared", name="moderation-service", node_kind="workload")
    db.commit()
    _alert_on(db, "squad-16-shared", "vm-kube-state-metrics", "fp-crash", "KubePodCrashLooping",
              'Pod squad-16-shared/moderation-service-5c5fbf559c-58chx (moderation-service) '
              'is in waiting state (reason: "CrashLoopBackOff") on cluster .')
    _alert_on(db, "monitoring", "vm-node-exporter", "fp-node", "NodeDiskIOSaturation",
              "Disk IO queue (aqu-sq) is high on md1 of node dev-6 (192.168.156.162:9100), …")
    _alert_on(db, "prod-kingdom5", "vm-kube-state-metrics", "fp-gone", "ProdPodStuckNotReady",
              "Дыра KubePodNotReady.\nkubectl -n prod-kingdom5 describe pod "
              "nats-exporter-prometheus-nats-exporter-686c945697-2n6ql\n")


@pytest.mark.parametrize("alertname, exporter, desc, expected", [
    ("KubeContainerWaiting", "vm-kube-state-metrics",
     "pod/chat-tasks-service-65b78b4dc4-b7tpt in namespace squad-30-kingdom5 on container x",
     (("squad-30-kingdom5", "chat-tasks-service"), "pod", None)),
    ("KubeNodePressure", "vm-kube-state-metrics",
     "dev-28 on cluster  has active Condition DiskPressure.", (None, "node", "dev-28")),
    ("NodeCPUHighUsage", "vm-node-exporter",
     "CPU usage at 192.168.148.16:9100 has been above 90%", (None, "node", None)),
    ("NodeDiskBelow20pctFree", "vm-node-exporter",
     "На узле infra-2 (192.168.14.66:9100) осталось < 20%", (None, "node", "infra-2")),
])
def test_resolve_target_from_description(db, alertname, exporter, desc, expected):
    upsert_service(db, namespace="squad-30-kingdom5", name="chat-tasks-service", node_kind="workload")
    db.commit()
    assert resolve_target(db, alertname, exporter, desc) == expected


def test_dry_run_reports_without_writing(db):
    _seed(db)
    st = reattribute(db, apply=False)
    assert st["candidates"] == 3
    assert st["to_service"] == 1 and st["to_node"] == 1 and st["unresolved"] == 1
    assert st["top_targets"] == {"squad-16-shared/moderation-service": 1}
    # Ничего не тронуто.
    ksm = db.query(Service).filter_by(name="vm-kube-state-metrics", namespace="squad-16-shared").one()
    assert db.query(AlertEvent).filter_by(fingerprint="fp-crash").one().service_id == ksm.id


def test_apply_moves_alerts_and_incidents_off_exporters(db):
    _seed(db)
    st = reattribute(db, apply=True)
    assert st["moved"] == 3 and st["failed"] == 0
    crash = db.query(AlertEvent).filter_by(fingerprint="fp-crash").one()
    assert db.get(Service, crash.service_id).name == "moderation-service"
    assert crash.raw["source"] == "vm-kube-state-metrics"
    assert crash.raw["reattributed_from"]["how"] == "pod"
    node = db.query(AlertEvent).filter_by(fingerprint="fp-node").one()
    assert node.service_id is None and node.raw["node"] == "dev-6"
    gone = db.query(AlertEvent).filter_by(fingerprint="fp-gone").one()
    assert gone.service_id is None and gone.raw["attribution"] == "pod_not_in_graph"
    # Инциденты экспортеров опустели и удалены. Для истории новый инцидент не
    # заводится: attach склеил бы старый эпизод с текущим открытым (ревью Codex).
    assert db.query(KGIncident).count() == 0
    assert crash.incident_id is None
    # Идемпотентность: повторно кандидатов нет.
    assert reattribute(db, apply=False)["candidates"] == 0


def test_firing_alert_is_moved_too(db):
    """Горящий алерт тоже переносится: store-upsert при повторе service_id не
    меняет, и без переноса строка осталась бы на экспортере навсегда."""
    upsert_service(db, namespace="squad-16-shared", name="moderation-service", node_kind="workload")
    db.commit()
    _alert_on(db, "squad-16-shared", "vm-kube-state-metrics", "fp-firing", "KubePodCrashLooping",
              "Pod squad-16-shared/moderation-service-5c5fbf559c-58chx (m) is in waiting state",
              resolved=False)
    st = reattribute(db, apply=True)
    assert st["candidates"] == 1 and st["firing"] == 1 and st["moved"] == 1
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-firing").one()
    assert db.get(Service, alert.service_id).name == "moderation-service"


def test_undo_restores_previous_service(db):
    _seed(db)
    ksm_ids = {a.fingerprint: a.service_id for a in db.query(AlertEvent).all()}
    reattribute(db, apply=True)
    st = undo(db, apply=True)
    assert st["restored"] == 3 and st["failed"] == 0
    for a in db.query(AlertEvent).all():
        assert a.service_id == ksm_ids[a.fingerprint]
        assert "reattributed_from" not in a.raw and "source" not in a.raw
        # Опустевшие инциденты экспортеров удалены при переносе — ссылку на
        # мёртвый key не восстанавливаем.
        assert a.incident_id is None
