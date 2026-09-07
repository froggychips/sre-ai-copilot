"""Job-алерты атрибутируются владельцу Job/CronJob, а не источнику метрики."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.job_attribution import (cronjob_base_name,
                                                 resolve_job_target)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent, K8sJob, KGIncident, Service
from app.models.incident import Incident
from app.scripts.reattribute_job_alerts import reattribute
from app.services.alert_enrichment import resolve_store_service

T0 = datetime(2026, 9, 7, 10, 0)


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


def _job(db, ns, name, kind, owner=None):
    db.add(K8sJob(namespace=ns, name=name, kind=kind, owner_service_name=owner))
    db.flush()


# ── чистые правила ────────────────────────────────────────────────────────

def test_cronjob_base_name_strips_unix_minute_suffix_only():
    assert cronjob_base_name("mcp-lastwar-wiki-sync-29779065") == "mcp-lastwar-wiki-sync"
    assert cronjob_base_name("squad-dashboard-29732425") == "squad-dashboard"
    assert cronjob_base_name("town-db-migrate") is None          # ad-hoc Job
    assert cronjob_base_name("backup-1234") is None               # короткий суффикс — не cron
    assert cronjob_base_name("") is None


def test_resolve_without_graph_uses_cronjob_name_or_job_name():
    assert resolve_job_target(None, "mcp", "mcp-lastwar-wiki-sync-29779065") == ("mcp-lastwar-wiki-sync", "cronjob_name")
    assert resolve_job_target(None, "squad-8-kingdom2", "town-db-migrate") == ("town-db-migrate", "job_name")
    assert resolve_job_target(None, "mcp", "") == (None, "job_name")


# ── с графом ──────────────────────────────────────────────────────────────

def test_resolve_prefers_job_owner_then_cronjob_owner_then_cronjob_name(db):
    _job(db, "mcp", "mcp-lastwar-wiki-sync-29779065", "job", owner="mcp-lastwar")
    _job(db, "mcp", "mcp-darkwar-wiki-sync", "cronjob", owner="mcp-darkwar")
    _job(db, "mcp", "mcp-squad-medic", "cronjob", owner=None)
    assert resolve_job_target(db, "mcp", "mcp-lastwar-wiki-sync-29779065") == ("mcp-lastwar", "job_owner")
    assert resolve_job_target(db, "mcp", "mcp-darkwar-wiki-sync-29779100") == ("mcp-darkwar", "cronjob_owner")
    assert resolve_job_target(db, "mcp", "mcp-squad-medic-29779100") == ("mcp-squad-medic", "cronjob_name")
    assert resolve_job_target(db, "mcp", "unknown-thing-29779100") == ("unknown-thing", "cronjob_name")


def test_store_resolver_never_returns_ksm_for_job_alerts(db):
    _job(db, "mcp", "mcp-lastwar-wiki-sync", "cronjob", owner="mcp-lastwar")
    labels = {"namespace": "mcp", "job_name": "mcp-lastwar-wiki-sync-29779065",
              "service": "vm-kube-state-metrics", "alertname": "KubeJobFailed"}
    assert resolve_store_service(labels, legacy_default=labels["service"], db=db) == "mcp-lastwar"
    assert resolve_store_service(labels, legacy_default=labels["service"]) == "mcp-lastwar-wiki-sync"


def test_store_resolver_deployment_label_still_wins_over_job_name(db):
    labels = {"namespace": "ns", "deployment": "map-service", "job_name": "map-service-29779065"}
    assert resolve_store_service(labels, legacy_default=None, db=db) == "map-service"


def test_populate_attributes_job_alert_to_owner_not_ksm(db):
    _job(db, "mcp", "mcp-lastwar-wiki-sync", "cronjob", owner="mcp-lastwar")
    inc = Incident(
        incident_id="fp-job", severity="warning", status="firing", summary="x",
        description="Job mcp/mcp-lastwar-wiki-sync-29779065 failed to complete.",
        namespace="mcp",
        labels={"alertname": "KubeJobFailed", "severity": "warning", "namespace": "mcp",
                "job_name": "mcp-lastwar-wiki-sync-29779065", "service": "vm-kube-state-metrics"},
        annotations={}, starts_at="2026-09-07T10:00:00Z",
    )
    populate_from_incident(db, inc)
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-job").one()
    svc = db.query(Service).filter_by(id=alert.service_id).one()
    assert svc.name == "mcp-lastwar" and svc.namespace == "mcp"
    assert db.query(KGIncident).one().service_name == "mcp-lastwar"


# ── переатрибуция накопленного ───────────────────────────────────────────

def _ksm_alert(db, ns, job, fp, fired_at=T0):
    ksm = upsert_service(db, namespace=ns, name="vm-kube-state-metrics")
    db.flush()
    db.add(AlertEvent(service_id=ksm.id, alertname="KubeJobFailed", severity="warning", fingerprint=fp,
                      fired_at=fired_at, raw={"description": f"Job {ns}/{job} failed to complete. Removing failed job."}))
    db.flush()
    from app.knowledge_graph.incidents import attach_alert
    attach_alert(db, namespace=ns, service_name="vm-kube-state-metrics", service_id=ksm.id,
                 fired_at=fired_at, alertname="KubeJobFailed", severity="warning", fingerprint=fp)
    db.commit()


def test_reattribute_dry_run_counts_and_apply_moves_alerts_and_incidents(db):
    _job(db, "mcp", "mcp-lastwar-wiki-sync", "cronjob", owner="mcp-lastwar")
    _ksm_alert(db, "mcp", "mcp-lastwar-wiki-sync-29779065", "fp-1")
    _ksm_alert(db, "mcp", "mcp-lastwar-wiki-sync-29779125", "fp-2")
    _ksm_alert(db, "sre-ai", "squad-dashboard-29732425", "fp-3")
    db.add(AlertEvent(service_id=db.query(Service).filter_by(namespace="mcp").first().id, alertname="KubeJobFailed",
                      severity="warning", fingerprint="fp-noraw", fired_at=T0, raw={"description": "no job here"}))
    db.commit()

    dry = reattribute(db, apply=False)
    assert dry["candidates"] == 4 and dry["resolved"] == 3 and dry["unparsed"] == 1
    assert dry["by_how"] == {"cronjob_owner": 2, "cronjob_name": 1}
    assert dry["moved"] == 0
    assert db.query(KGIncident).count() == 2                      # ничего не тронуто

    out = reattribute(db, apply=True)
    assert out["moved"] == 3
    a1 = db.query(AlertEvent).filter_by(fingerprint="fp-1").one()
    assert db.query(Service).filter_by(id=a1.service_id).one().name == "mcp-lastwar"
    a3 = db.query(AlertEvent).filter_by(fingerprint="fp-3").one()
    assert db.query(Service).filter_by(id=a3.service_id).one().name == "squad-dashboard"
    incs = {(i.namespace, i.service_name): i for i in db.query(KGIncident).all()}
    assert ("mcp", "mcp-lastwar") in incs and incs[("mcp", "mcp-lastwar")].alert_count == 2
    assert ("sre-ai", "squad-dashboard") in incs
    # Оба KSM-инцидента опустели и удалены: mcp — после второго перенесённого
    # алерта (первый его только «ужал»), sre-ai — сразу.
    assert ("sre-ai", "vm-kube-state-metrics") not in incs
    assert ("mcp", "vm-kube-state-metrics") not in incs
    assert out["incidents_deleted"] == 2 and out["incidents_shrunk"] == 1
    assert a1.incident_id == incs[("mcp", "mcp-lastwar")].incident_key
    # Неразобранный алерт остался на KSM-сервисе — его переносить нечем.
    noraw = db.query(AlertEvent).filter_by(fingerprint="fp-noraw").one()
    assert db.query(Service).filter_by(id=noraw.service_id).one().name == "vm-kube-state-metrics"
