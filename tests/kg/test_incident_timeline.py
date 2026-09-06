"""Timeline инцидента: пять таблиц графа на одной оси времени + Known Unknowns."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.incident_timeline import LOOKBACK_MIN, build_timeline
from app.knowledge_graph.incidents import attach_alert, reconcile_incidents
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import (AlertEvent, AnomalyObservation,
                                        Deployment, KGIncident, LogObservation,
                                        PodEvent)

T0 = datetime(2026, 9, 6, 10, 0, 0)
M = timedelta(minutes=1)


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


def _seed(db):
    svc = upsert_service(db, namespace="squad-1", name="town-service")
    db.flush()
    # ns-broadcast деплой за 40 мин — «в namespace катили», к сервису не привязан.
    db.add(Deployment(service_id=svc.id, started_at=T0 - 40 * M, buildtype_id="Wo_Build", build_number="100",
                      status="SUCCESS", extras={"namespace_scope": True}))
    # точный выкат в кластере за 20 мин.
    db.add(Deployment(service_id=svc.id, started_at=T0 - 20 * M, buildtype_id="k8s_rollout",
                      build_number="uid:hash", extras={"namespace_scope": False, "attribution": "k8s_rollout",
                                                       "rollout_reason": "image", "images": ["svc:2"],
                                                       "previous_images": ["svc:1"]}))
    # деплой вне окна (2 часа назад) — в ленту не попадает.
    db.add(Deployment(service_id=svc.id, started_at=T0 - 120 * M, buildtype_id="Old", build_number="1",
                      extras={"namespace_scope": False}))
    db.add(PodEvent(service_id=svc.id, namespace="squad-1", pod_name="town-service-abc-xyz",
                    reason="BackOff", message="restarting failed container", type="Warning",
                    event_uid="e1", first_seen=T0 - 10 * M, last_seen=T0 - 8 * M, count=4))
    for i, z in enumerate((5.0, 9.5, 7.0)):
        db.add(AnomalyObservation(service_id=svc.id, ts=T0 - (5 - i) * M, metric="cpu_pct",
                                  value=60.0 + i, baseline_mean=3.2, baseline_stddev=0.3, z_score=z,
                                  severity="critical" if z > 6 else "warning", notified=False))
    db.add(LogObservation(service_id=svc.id, ts=T0 - 2 * M, level="Error", count=12))
    db.add(LogObservation(service_id=svc.id, ts=T0 - 2 * M, level="Information", count=500))  # шум — не берём
    db.add(AlertEvent(service_id=svc.id, alertname="PodCrashLooping", severity="critical",
                      fingerprint="fp-1", fired_at=T0))
    db.flush()
    inc = attach_alert(db, namespace="squad-1", service_name="town-service", service_id=svc.id,
                       fired_at=T0, alertname="PodCrashLooping", severity="critical", fingerprint="fp-1")
    return svc, inc


def test_timeline_puts_five_sources_on_one_axis_in_causal_order(db):
    _svc, inc = _seed(db)
    tl = build_timeline(db, inc, now=T0 + 5 * M)
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == [
        "deploy", "deploy", "pod_event", "anomaly", "log_errors", "incident.opened", "alert.fired",
    ]
    assert tl["counts"] == {"deploy": 2, "pod_event": 1, "anomaly": 1, "log_errors": 1,
                            "incident.opened": 1, "alert.fired": 1}
    assert tl["window"]["start"] == T0 - LOOKBACK_MIN * M
    assert tl["unknowns"] == []


def test_deploy_events_carry_attribution_as_evidence(db):
    _svc, inc = _seed(db)
    deploys = [e for e in build_timeline(db, inc, now=T0 + 5 * M)["events"] if e["kind"] == "deploy"]
    broadcast, exact = deploys
    assert broadcast["evidence"] == {"epistemic": "inferred", "provenance": "kg_deployments/namespace"}
    assert "привязка к сервису не подтверждена" in broadcast["title"]
    assert exact["evidence"] == {"epistemic": "observed", "provenance": "kg_deployments/service"}
    assert exact["details"]["rollout_reason"] == "image" and exact["details"]["images"] == ["svc:2"]
    assert exact["title"].startswith("Выкат в кластере")


def test_anomalies_are_bucketed_per_metric_and_hour(db):
    _svc, inc = _seed(db)
    anomaly = next(e for e in build_timeline(db, inc, now=T0 + 5 * M)["events"] if e["kind"] == "anomaly")
    assert anomaly["details"]["count"] == 3
    assert anomaly["details"]["max_abs_z"] == 9.5
    assert anomaly["details"]["severity"] == "critical"
    assert anomaly["evidence"]["epistemic"] == "inferred"      # вывод детектора, не наблюдение


def test_only_error_level_logs_make_the_timeline(db):
    _svc, inc = _seed(db)
    logs = [e for e in build_timeline(db, inc, now=T0 + 5 * M)["events"] if e["kind"] == "log_errors"]
    assert len(logs) == 1 and logs[0]["details"] == {"level": "Error", "count": 12}


def test_resolution_closes_the_timeline(db):
    _svc, inc = _seed(db)
    db.query(AlertEvent).filter_by(fingerprint="fp-1").update({"resolved_at": T0 + 15 * M})
    db.flush()
    reconcile_incidents(db, now=T0 + 16 * M)
    db.refresh(inc)
    tl = build_timeline(db, inc, now=T0 + 60 * M)
    tail = [e["kind"] for e in tl["events"]][-2:]
    assert tail == ["alert.resolved", "incident.resolved"]
    resolved = tl["events"][-1]
    assert resolved["details"] == {"reason": "all_alerts_resolved", "duration_min": 15}
    assert tl["window"]["end"] == T0 + 45 * M      # resolved_at + lookahead


def test_incident_without_service_in_graph_reports_known_unknowns(db):
    db.add(AlertEvent(alertname="X", severity="warning", fingerprint="fp-9", fired_at=T0))
    db.flush()
    inc = attach_alert(db, namespace="ghost", service_name="nobody", service_id=None,
                       fired_at=T0, alertname="X", severity="warning", fingerprint="fp-9")
    tl = build_timeline(db, inc, now=T0 + M)
    assert [e["kind"] for e in tl["events"]] == ["incident.opened", "alert.fired"]
    assert len(tl["unknowns"]) == 1
    assert "service_id" in tl["unknowns"][0]["reason"]
    assert "deploy" in tl["unknowns"][0]["scope"]


def test_incident_payload_is_included(db):
    _svc, inc = _seed(db)
    tl = build_timeline(db, inc, now=T0 + M)
    assert tl["incident"]["incident_key"] == inc.incident_key
    assert tl["incident"]["alert_count"] == 1
    assert isinstance(db.query(KGIncident).one().id, int)
