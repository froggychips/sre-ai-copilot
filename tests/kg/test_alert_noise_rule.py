"""Статическое правило шума: CPUThrottlingHigh на стендах не плодит инциденты.

Покрытие: само правило (чистая функция), жизненный цикл инцидента с
`noise_kind` (хронический инцидент, без переоткрытий, вытеснение сигналом),
сквозной путь через populate_from_incident и читатели kg_alerts
(stuck-alerts, team-digest, chronic-digest).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.incidents import (NOISE_KEY_SUFFIX, RESOLVE_SUPERSEDED,
                                           STATUS_OPEN, STATUS_RESOLVED,
                                           attach_alert, mark_incident_noise,
                                           reconcile_incidents)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import AlertEvent, KGIncident
from app.knowledge_graph.stuck_alerts import find_stuck_alerts
from app.models.incident import Incident
from app.services.alert_noise_rule import (NOISE_KIND_RULE, RAW_NOISE_KEY,
                                           noise_rule_kind)

T0 = datetime(2026, 11, 18, 10, 0, 0)
THROTTLE = "CPUThrottlingHigh"


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


def _svc(db, ns="squad-1-shared", name="town-db-postgresql"):
    svc = upsert_service(db, namespace=ns, name=name)
    db.flush()
    return svc


def _attach(db, svc, fp, fired_at, alertname=THROTTLE, severity="info", noise=True):
    if db.query(AlertEvent).filter(AlertEvent.fingerprint == fp).one_or_none() is None:
        db.add(AlertEvent(service_id=svc.id, alertname=alertname, severity=severity,
                          fingerprint=fp, fired_at=fired_at))
        db.flush()
    return attach_alert(
        db, namespace=svc.namespace, service_name=svc.name, service_id=svc.id,
        fired_at=fired_at, alertname=alertname, severity=severity, fingerprint=fp,
        noise_kind=NOISE_KIND_RULE if noise else None,
    )


def _resolve(db, fp, at):
    db.query(AlertEvent).filter(AlertEvent.fingerprint == fp).update({"resolved_at": at})
    db.flush()
    reconcile_incidents(db, now=at)


# ── Правило ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("labels,expected", [
    ({"alertname": THROTTLE, "namespace": "squad-12-shared", "container": "metrics"}, NOISE_KIND_RULE),
    ({"alertname": THROTTLE, "namespace": "squad-gd-kingdom2", "container": "postgresql"}, NOISE_KIND_RULE),
    ({"alertname": THROTTLE, "namespace": "dev-tools"}, NOISE_KIND_RULE),
    ({"alertname": THROTTLE, "namespace": "prod-kingdom5", "container": "metrics"}, None),
    ({"alertname": THROTTLE, "namespace": "preupdate-shared", "container": "metrics"}, None),
    ({"alertname": "KubePodCrashLooping", "namespace": "squad-12-shared", "container": "metrics"}, None),
    ({"alertname": THROTTLE}, None),
])
def test_default_rule(labels, expected):
    assert noise_rule_kind(labels) == expected


def test_rule_kill_switch(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_ENABLED", False)
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": "squad-1-shared"}) is None


def test_rule_container_patterns(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_CONTAINERS", ["metrics", "*-backup"])
    ns = "squad-1-shared"
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": ns, "container": "metrics"})
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": ns, "container": "message-db-backup"})
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": ns, "container": "postgresql"}) is None
    # Лейбла container нет — сравнивается имя сервиса.
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": ns}, service="metrics")
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": ns}, service="town-service") is None


def test_rule_container_in_any_namespace(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_CONTAINERS_ANY_NAMESPACE", ["metrics"])
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": "prod-kingdom5", "container": "metrics"})
    assert noise_rule_kind({"alertname": THROTTLE, "namespace": "prod-kingdom5", "container": "postgresql"}) is None


def test_namespace_argument_wins_over_label():
    # populate передаёт namespace цели (target.namespace), а не лейбл скрейпа.
    labels = {"alertname": THROTTLE, "namespace": "monitoring"}
    assert noise_rule_kind(labels, "squad-3-kingdom1") == NOISE_KIND_RULE


# ── Жизненный цикл инцидента ──────────────────────────────────────────────


def test_noise_alert_opens_noise_incident(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-t", T0)
    assert inc.noise is True
    assert inc.status == STATUS_OPEN
    assert inc.incident_key.endswith(NOISE_KEY_SUFFIX)
    assert inc.extras["noise_fingerprints"] == {"fp-t": [NOISE_KIND_RULE]}
    assert db.query(AlertEvent).filter_by(fingerprint="fp-t").one().incident_id == inc.incident_key


def test_flapping_noise_stays_one_incident_without_reopens(db):
    """Флап раз в час в течение суток: было бы 24 инцидента, остаётся один."""
    svc = _svc(db)
    first = _attach(db, svc, "fp-t", T0)
    for hour in range(1, 24):
        _resolve(db, "fp-t", T0 + timedelta(hours=hour, minutes=-20))
        assert db.get(KGIncident, first.id).status == STATUS_RESOLVED
        db.query(AlertEvent).filter_by(fingerprint="fp-t").update({"resolved_at": None})
        again = _attach(db, svc, "fp-t", T0 + timedelta(hours=hour))
        assert again.id == first.id
    rows = db.query(KGIncident).all()
    assert len(rows) == 1
    assert rows[0].reopened_count == 0
    assert rows[0].extras["noise_refires"] == 23
    assert rows[0].noise is True and rows[0].status == STATUS_OPEN


def test_noise_reopen_window_is_bounded(db, monkeypatch):
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_REOPEN_HOURS", 2)
    svc = _svc(db)
    first = _attach(db, svc, "fp-t", T0)
    _resolve(db, "fp-t", T0 + timedelta(minutes=10))
    db.query(AlertEvent).filter_by(fingerprint="fp-t").update({"resolved_at": None})
    later = _attach(db, svc, "fp-t", T0 + timedelta(hours=5))
    assert later.id != first.id
    assert later.noise is True


def test_signal_alert_supersedes_open_noise_incident(db):
    """CrashLoop на сервисе с хроническим троттлингом получает свой инцидент."""
    svc = _svc(db)
    noise = _attach(db, svc, "fp-t", T0)
    signal = _attach(db, svc, "fp-c", T0 + timedelta(days=3),
                     alertname="KubePodCrashLooping", severity="warning", noise=False)
    assert signal.id != noise.id
    assert signal.noise is False
    assert signal.alertnames == ["KubePodCrashLooping"]
    assert signal.opened_at == T0 + timedelta(days=3)
    closed = db.get(KGIncident, noise.id)
    assert closed.status == STATUS_RESOLVED
    assert closed.resolve_reason == RESOLVE_SUPERSEDED


def test_signal_and_noise_with_same_starts_at_do_not_collide(db):
    """Общий такт оценки правил: одинаковый startsAt у обоих алертов."""
    svc = _svc(db)
    noise = _attach(db, svc, "fp-t", T0)
    signal = _attach(db, svc, "fp-c", T0, alertname="KubePodCrashLooping", noise=False)
    assert signal.id != noise.id
    assert signal.incident_key != noise.incident_key


def test_signal_alert_does_not_reopen_resolved_noise_incident(db):
    svc = _svc(db)
    noise = _attach(db, svc, "fp-t", T0)
    _resolve(db, "fp-t", T0 + timedelta(minutes=5))
    signal = _attach(db, svc, "fp-c", T0 + timedelta(minutes=10),
                     alertname="KubePodCrashLooping", noise=False)
    assert signal.id != noise.id
    assert signal.reopened_count == 0
    assert db.get(KGIncident, noise.id).reopened_count == 0


def test_noise_alert_does_not_reopen_resolved_signal_incident(db):
    svc = _svc(db)
    signal = _attach(db, svc, "fp-c", T0, alertname="KubePodCrashLooping", noise=False)
    _resolve(db, "fp-c", T0 + timedelta(minutes=5))
    noise = _attach(db, svc, "fp-t", T0 + timedelta(minutes=10))
    assert noise.id != signal.id
    assert noise.noise is True
    assert db.get(KGIncident, signal.id).status == STATUS_RESOLVED
    assert db.get(KGIncident, signal.id).reopened_count == 0


def test_noise_alert_joins_open_signal_incident_without_hiding_it(db):
    svc = _svc(db)
    signal = _attach(db, svc, "fp-c", T0, alertname="KubePodCrashLooping", noise=False)
    same = _attach(db, svc, "fp-t", T0 + timedelta(minutes=1))
    assert same.id == signal.id
    assert same.noise is False
    assert same.extras["noise_fingerprints"] == {"fp-t": [NOISE_KIND_RULE]}


def test_rule_mark_is_removed_when_rule_stops_matching(db):
    svc = _svc(db)
    inc = _attach(db, svc, "fp-t", T0)
    assert inc.noise is True
    again = _attach(db, svc, "fp-t", T0 + timedelta(minutes=5), noise=False)
    # Тот же fingerprint: инцидент тот же, но уже не шум и не «чисто шумовой».
    assert again.id == inc.id
    assert again.noise is False
    assert again.extras["noise_fingerprints"] == {}


def test_other_noise_kinds_keep_regular_reopen(db):
    """Шум обогащения (rollout и т.п.) живёт по прежним правилам переоткрытия."""
    svc = _svc(db, ns="prod-kingdom1", name="town-service")
    inc = _attach(db, svc, "fp-g", T0, alertname="KubeDeploymentGenerationMismatch", noise=False)
    mark_incident_noise(db, fingerprint="fp-g", kinds=["rollout"])
    _resolve(db, "fp-g", T0 + timedelta(minutes=5))
    db.query(AlertEvent).filter_by(fingerprint="fp-g").update({"resolved_at": None})
    again = _attach(db, svc, "fp-g", T0 + timedelta(minutes=10),
                    alertname="KubeDeploymentGenerationMismatch", noise=False)
    assert again.id == inc.id
    assert again.reopened_count == 1


# ── Сквозной путь и читатели ──────────────────────────────────────────────


def _am_incident(fp, ns, alertname=THROTTLE, service="town-db-postgresql",
                 container="metrics", starts_at="2026-11-18T10:00:00Z"):
    return Incident(
        incident_id=fp, severity="info", status="firing", summary="x", description="y",
        namespace=ns,
        labels={"alertname": alertname, "service": service, "namespace": ns,
                "container": container, "severity": "info"},
        annotations={}, starts_at=starts_at,
    )


def test_populate_marks_alert_and_incident(db):
    stats = populate_from_incident(db, _am_incident("fp-1", "squad-7-shared"))
    db.commit()
    assert stats["noise_rule"] == 1
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-1").one()
    assert alert.raw[RAW_NOISE_KEY] == NOISE_KIND_RULE
    assert db.query(KGIncident).one().noise is True


def test_populate_leaves_prod_and_other_alerts_alone(db):
    populate_from_incident(db, _am_incident("fp-p", "prod-kingdom5"))
    populate_from_incident(db, _am_incident("fp-c", "squad-7-shared", alertname="KubePodCrashLooping",
                                            service="town-service"))
    db.commit()
    for fp in ("fp-p", "fp-c"):
        assert RAW_NOISE_KEY not in db.query(AlertEvent).filter_by(fingerprint=fp).one().raw
    assert [i.noise for i in db.query(KGIncident).all()] == [False, False]


def test_populate_respects_kill_switch(db, monkeypatch):
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_ENABLED", False)
    stats = populate_from_incident(db, _am_incident("fp-1", "squad-7-shared"))
    db.commit()
    assert "noise_rule" not in stats
    inc = db.query(KGIncident).one()
    assert inc.noise is False
    assert not inc.incident_key.endswith(NOISE_KEY_SUFFIX)


def _old_open_alert(db, svc, fp, alertname, raw=None, hours=30.0):
    now = datetime.utcnow()
    db.add(AlertEvent(service_id=svc.id, alertname=alertname, severity="info", fingerprint=fp,
                      fired_at=now - timedelta(hours=hours), last_notified_at=now, raw=raw))
    db.commit()


def test_stuck_alerts_skip_rule_noise(db, monkeypatch):
    svc = _svc(db)
    _old_open_alert(db, svc, "fp-t", THROTTLE, raw={RAW_NOISE_KEY: NOISE_KIND_RULE})
    _old_open_alert(db, svc, "fp-c", "KubePodCrashLooping", raw={"description": "x"})
    _old_open_alert(db, svc, "fp-n", "KubeJobFailed", raw=None)
    assert sorted(s["alertname"] for s in find_stuck_alerts(db)) == ["KubeJobFailed", "KubePodCrashLooping"]
    monkeypatch.setattr(settings, "ALERT_NOISE_RULE_EXCLUDE_FROM_REPORTS", False)
    assert len(find_stuck_alerts(db)) == 3


def test_team_digest_open_alerts_skip_rule_noise(db):
    from app.services.team_digest import _alerts_breakdown

    svc = upsert_service(db, namespace="squad-1-shared", name="town-db-postgresql", team_owner="squad-1")
    db.flush()
    _old_open_alert(db, svc, "fp-t", THROTTLE, raw={RAW_NOISE_KEY: NOISE_KIND_RULE}, hours=2)
    _old_open_alert(db, svc, "fp-c", "KubePodCrashLooping", raw={"description": "x"}, hours=2)
    out = _alerts_breakdown(db, "squad-1", datetime.utcnow() - timedelta(hours=24))
    assert out["open_total"] == 1
    assert out["top_alertnames"] == [("KubePodCrashLooping", 1)]


def test_chronic_digest_skips_rule_noise(db):
    from app.services.chronic_digest import _aggregate, _continuous

    svc = _svc(db)
    for i in range(3):
        _old_open_alert(db, svc, f"fp-t{i}", THROTTLE, raw={RAW_NOISE_KEY: NOISE_KIND_RULE})
        _old_open_alert(db, svc, f"fp-c{i}", "KubePodCrashLooping", raw={"description": "x"})
        _old_open_alert(db, svc, f"fp-n{i}", "KubeJobFailed", raw=None)
    agg = _aggregate(db, window_hours=48, min_fires=3)
    assert sorted(r["alertname"] for r in agg) == ["KubeJobFailed", "KubePodCrashLooping"]
    cont = _continuous(db, min_hours=24)
    assert THROTTLE not in {r["alertname"] for r in cont}
    assert len(cont) == 6
