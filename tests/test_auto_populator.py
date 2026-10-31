"""Тесты на auto_populator — наполнение KG из инцидентов."""
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.auto_populator import populate_from_incident
from app.knowledge_graph.queries import (incidents_on, nearby_alerts,
                                         recent_deploys_for)
from app.knowledge_graph.schema import (AlertEvent, KGIncident, Service,
                                        ServiceEdge)  # noqa: F401
from app.models.incident import Incident


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _incident(
    incident_id="inc-1", service="town-service", namespace="squad-1",
    starts_at="2026-05-12T10:00:00Z", tc=None,
):
    return Incident(
        incident_id=incident_id,
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace=namespace,
        labels={"alertname": "HighLatency", "service": service,
                "severity": "warning"},
        annotations={},
        starts_at=starts_at,
        teamcity_context=tc,
    )


def test_populate_creates_service_and_alert(db):
    stats = populate_from_incident(db, _incident())
    assert stats["services_touched"] == 1
    assert stats["alerts_added"] == 1
    assert stats["deploys_added"] == 0

    svc = db.query(Service).filter(Service.name == "town-service").one()
    assert svc.namespace == "squad-1"

    alerts = incidents_on(
        db, "squad-1", "town-service",
        since=datetime(2026, 5, 12, 9, 0),
        until=datetime(2026, 5, 12, 11, 0),
    )
    assert len(alerts) == 1
    assert alerts[0]["fingerprint"] == "inc-1"


def test_populate_persists_batch_truncation_in_alert_raw(db):
    """Усечённый batch AM виден в самой строке kg_alerts, а полный — нет.

    raw перезаписывается каждым уведомлением: следующий полный batch того же
    fingerprint-а должен снять пометку, а не тащить её вечно.
    """
    inc = _incident(incident_id="trunc-1")
    inc.batch_truncated_alerts = 9
    populate_from_incident(db, inc)
    db.commit()
    row = db.query(AlertEvent).filter(AlertEvent.fingerprint == "trunc-1").one()
    assert row.raw == {"description": "y", "batch_truncated_alerts": 9}

    populate_from_incident(db, _incident(incident_id="trunc-1"))
    db.commit()
    db.refresh(row)
    assert row.raw == {"description": "y"}


def test_populate_records_deployments_from_tc(db):
    # auto_populator достаёт sha из changes[0].version (фикс 039c80c) — TC-context
    # не выдаёт sha напрямую на верхнем уровне билда, только через changes.
    tc = {
        "recent_builds": [
            {
                "number": "1234", "status": "SUCCESS",
                "buildtype_id": "Wo_Backend_Town",
                "branch": "master",
                "finished_at": "2026-05-12T09:30:00Z",
                "started_at": "2026-05-12T09:25:00Z",
                "changes": [{"version": "abc1234"}],
            },
            {
                "number": "1235", "status": "RUNNING",
                "buildtype_id": "Wo_Backend_Town",
                "started_at": "2026-05-12T09:55:00Z",
            },
        ]
    }
    stats = populate_from_incident(db, _incident(tc=tc))
    assert stats["deploys_added"] == 2

    deploys = recent_deploys_for(
        db, "squad-1", "town-service",
        before=datetime(2026, 5, 12, 10, 0),
        lookback_minutes=120,
    )
    shas = {d["sha"] for d in deploys}
    assert "abc1234" in shas


def test_populate_deploy_not_lost_on_unparseable_finished_at(db):
    """KG H1: непустой, но непарсящийся finished_at не должен терять деплой.

    До фикса `_parse_ts(...).replace()` бросал AttributeError (None.replace),
    деплой молча проваливался в except. started_at валиден → деплой обязан
    записаться с finished_at=None.
    """
    tc = {
        "recent_builds": [
            {
                "number": "1300", "status": "RUNNING",
                "buildtype_id": "Wo_Backend_Town",
                "started_at": "2026-05-12T09:25:00Z",
                "finished_at": "in-progress",  # непустой, но не ISO-таймстамп
                "changes": [{"version": "deadc0de"}],
            },
        ]
    }
    stats = populate_from_incident(db, _incident(tc=tc))
    assert stats["deploys_added"] == 1
    deploys = recent_deploys_for(
        db, "squad-1", "town-service",
        before=datetime(2026, 5, 12, 10, 0), lookback_minutes=120,
    )
    assert "deadc0de" in {d["sha"] for d in deploys}


def _serviceless(incident_id, namespace, labels):
    return Incident(
        incident_id=incident_id, severity="warning", status="firing",
        summary="x", description="y", namespace=namespace, labels=labels,
        annotations={}, starts_at="2026-05-12T10:00:00Z",
    )


def test_populate_records_alert_without_service_label(db):
    """Алерт без сервиса — всё равно факт алерта. До 16.10.2026 ранний return
    терял его целиком; Service при этом не придумываем."""
    stats = populate_from_incident(db, _serviceless(
        "inc-noservice", "squad-1", {"alertname": "GenericAlert"}))
    assert stats["services_touched"] == 0 and stats["alerts_added"] == 1
    alert = db.query(AlertEvent).filter_by(fingerprint="inc-noservice").one()
    assert alert.service_id is None
    assert db.query(Service).count() == 0
    # Namespace без сервиса иначе теряется — по нему сверка покрытия
    # (weekly_reconciliation) ключует алерт парой (alertname, namespace).
    assert alert.raw["namespace"] == "squad-1"


def test_node_only_alert_is_persisted_without_service(db):
    """Нодовый алерт без namespace: raw.node нужен #477 (скоуп kg_nodes),
    инцидента сервиса нет — нода не притворяется сервисом."""
    stats = populate_from_incident(db, _serviceless(
        "fp-node-1", None,
        {"alertname": "NodeNotReady", "node": "worker-17", "severity": "critical"}))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-node-1").one()
    assert alert.service_id is None
    assert alert.raw["node"] == "worker-17"
    assert alert.severity == "critical"
    assert stats["alerts_added"] == 1 and stats["alerts_without_service"] == 1
    assert "kg_incident_id" not in stats
    assert db.query(KGIncident).count() == 0


def test_scrape_namespace_without_service_builds_no_phantom_service(db):
    """namespace=monitoring у нодового алерта — метка скрейпа, а не scope."""
    populate_from_incident(db, _serviceless(
        "fp-node-2", "monitoring", {"alertname": "NodeDiskPressure", "node": "worker-17"}))
    assert db.query(Service).count() == 0
    assert db.query(AlertEvent).filter_by(fingerprint="fp-node-2").one().raw["node"] == "worker-17"


def test_service_upsert_failure_does_not_write_orphan_alert(db, monkeypatch):
    """Сервис у алерта есть, запись не удалась — алерт без сервиса не пишем:
    повторные уведомления service_id не обновляют, строка осталась бы ничьей."""
    import app.knowledge_graph.auto_populator as ap

    def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(ap, "upsert_service", boom)
    stats = populate_from_incident(db, _incident(incident_id="fp-svc-fail"))
    assert stats["alerts_added"] == 0
    assert db.query(AlertEvent).count() == 0


def test_populate_idempotent_on_second_call(db):
    """Повторный прогон не дублирует service и не дублирует AlertEvent."""
    populate_from_incident(db, _incident())
    populate_from_incident(db, _incident())  # тот же fingerprint

    assert db.query(Service).count() == 1
    assert db.query(AlertEvent).count() == 1


def test_populate_picks_app_label_when_no_service(db):
    """Если labels.service нет, но есть labels.app — используем его как service-name."""
    incident = Incident(
        incident_id="inc-app",
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace="squad-1",
        labels={"alertname": "X", "app": "auth-svc"},
        annotations={},
        starts_at="2026-05-12T10:00:00Z",
    )
    populate_from_incident(db, incident)
    assert db.query(Service).filter(Service.name == "auth-svc").one() is not None


# ──────────────────────────────────────────────────────────────────────────
# Kube-resource alert attribution (баг «vm-kube-state-metrics noisemaker»).
# У KubeDeployment*/StatefulSet*/DaemonSet*-алертов лейбл `service` = метрика-
# источник kube-state-metrics, не target. STORE-путь должен писать в kg_alerts
# target-workload, не vm-kube-state-metrics.
# ──────────────────────────────────────────────────────────────────────────


def _kube_alert_incident(incident_id="inc-genmismatch"):
    return Incident(
        incident_id=incident_id,
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace="prod-kingdom4",
        labels={
            "alertname": "KubeDeploymentGenerationMismatch",
            "namespace": "prod-kingdom4",
            "deployment": "map-service",
            "service": "vm-kube-state-metrics",  # метрика-источник KSM, не target
            "severity": "warning",
        },
        annotations={},
        starts_at="2026-07-02T10:00:00Z",
    )


def test_populate_kube_alert_resolves_target_not_ksm_service(db):
    """(a) gen-mismatch: deployment=map-service + service=vm-kube-state-metrics
    → в kg_alerts сервис резолвится в map-service, KSM-источника в графе нет."""
    stats = populate_from_incident(db, _kube_alert_incident())
    assert stats["services_touched"] == 1
    assert stats["alerts_added"] == 1

    svc = db.query(Service).one()
    assert svc.name == "map-service"
    assert svc.namespace == "prod-kingdom4"
    assert (
        db.query(Service).filter(Service.name == "vm-kube-state-metrics").count() == 0
    )


def test_populate_app_alert_keeps_service_label(db):
    """(b) app-алерт: валидный labels.service, нет kube-resource-лейблов →
    поведение прежнее (service остаётся auth-service)."""
    inc = Incident(
        incident_id="inc-app-auth",
        severity="warning",
        status="firing",
        summary="x",
        description="y",
        namespace="prod-shared",
        labels={
            "alertname": "HighErrorRate",
            "namespace": "prod-shared",
            "service": "auth-service",
            "severity": "warning",
        },
        annotations={},
        starts_at="2026-07-02T10:00:00Z",
    )
    populate_from_incident(db, inc)
    assert db.query(Service).one().name == "auth-service"


def test_populate_kube_alert_kill_switch_off_keeps_legacy_ksm(db):
    """(c) ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED=False → прежнее (баг-)поведение:
    лейбл `service` в приоритете → vm-kube-state-metrics."""
    with patch(
        "app.services.alert_enrichment.settings.ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED",
        False,
    ):
        populate_from_incident(db, _kube_alert_incident("inc-genmismatch-off"))
    assert db.query(Service).one().name == "vm-kube-state-metrics"


def test_upsert_edge_merges_extras_does_not_overwrite(db):
    """Когда runtime-источник (OTEL spans, VM client-requests) перезапишет
    edge — он должен ДОПОЛНИТЬ extras (добавить confidence='runtime_seen'),
    а не стереть существующие inferred-аннотации."""
    from app.knowledge_graph.populator import upsert_edge, upsert_service

    town = upsert_service(db, namespace="ns1", name="town")
    auth = upsert_service(db, namespace="ns1", name="auth")

    # 1-й pass: env-derived
    upsert_edge(
        db, town, auth, kind="calls",
        extras={"confidence": "inferred_env", "semantics": "sync"},
    )
    edge = db.query(ServiceEdge).filter_by(src_id=town.id, dst_id=auth.id).one()
    assert edge.extras == {"confidence": "inferred_env", "semantics": "sync"}

    # 2-й pass: runtime-источник видит то же ребро + добавляет traffic_share
    upsert_edge(
        db, town, auth, kind="calls",
        extras={"confidence": "runtime_seen", "traffic_share": 0.92},
    )
    edge2 = db.query(ServiceEdge).filter_by(src_id=town.id, dst_id=auth.id).one()
    # confidence перезаписан (более поздний источник — более доверенный),
    # semantics СОХРАНЕНА (не было в new extras → merge), traffic_share добавлен.
    assert edge2.extras == {
        "confidence": "runtime_seen",
        "semantics": "sync",
        "traffic_share": 0.92,
    }


def test_populate_after_full_cycle_enables_nearby_alerts(db):
    """E2E: 2 инцидента на разные сервисы + edge между ними → nearby_alerts работает."""
    populate_from_incident(db, _incident(
        incident_id="auth-down", service="auth", namespace="squad-1",
        starts_at="2026-05-12T09:57:00Z",
    ))
    populate_from_incident(db, _incident(
        incident_id="town-down", service="town-service", namespace="squad-1",
        starts_at="2026-05-12T10:00:00Z",
    ))

    # Создаём edge town → auth (вручную, потому что populator edges не делает).
    from app.knowledge_graph.populator import upsert_edge
    town = db.query(Service).filter(Service.name == "town-service").one()
    auth = db.query(Service).filter(Service.name == "auth").one()
    upsert_edge(db, town, auth, kind="calls")

    nearby = nearby_alerts(
        db, "squad-1", "town-service",
        around=datetime(2026, 5, 12, 10, 0),
        window_minutes=15,
    )
    assert len(nearby) == 1
    assert nearby[0]["service"] == "auth"
    assert nearby[0]["minutes_before"] == 3


# ──────────────────────────────────────────────────────────────────────────
# KG H2: один битый item не должен валить весь tick и отравлять Session.
# ──────────────────────────────────────────────────────────────────────────
# Регресс на P1: в except-ветках per-item циклов не было SAVEPOINT-изоляции,
# поэтому первая IntegrityError/DataError переводила PG-сессию в aborted —
# все последующие записи и финальный db.commit() падали с PendingRollbackError.
# Фикс: каждый populator-вызов обёрнут в db.begin_nested() (SAVEPOINT), его
# контекст-менеджер откатывает только битый item. Проверяем на SQLite — он
# поддерживает SAVEPOINT, и логика отката та же, что у PG.


def test_populate_one_bad_deploy_does_not_kill_batch(db, monkeypatch):
    """Битый build в середине списка НЕ роняет соседние builds и alert,
    а Session остаётся пригодной для commit (нет PendingRollbackError)."""
    import app.knowledge_graph.auto_populator as ap

    real_record_deployment = ap.record_deployment
    calls = {"n": 0}

    def flaky_record_deployment(db, *, service, **kwargs):
        calls["n"] += 1
        # Роняем ровно второй build (number=1235) — внутри SAVEPOINT.
        if kwargs.get("build_number") == "1235":
            raise RuntimeError("simulated DataError on build 1235")
        return real_record_deployment(db, service=service, **kwargs)

    monkeypatch.setattr(ap, "record_deployment", flaky_record_deployment)

    # Спай на begin_nested: подтверждаем, что каждый build пишется внутри
    # SAVEPOINT (на PG именно это спасает Session от aborted-состояния).
    nested_calls = {"n": 0}
    real_begin_nested = db.begin_nested

    def spy_begin_nested():
        nested_calls["n"] += 1
        return real_begin_nested()

    monkeypatch.setattr(db, "begin_nested", spy_begin_nested)

    tc = {
        "recent_builds": [
            {"number": "1234", "status": "SUCCESS", "buildtype_id": "Wo_Backend_Town",
             "started_at": "2026-05-12T09:25:00Z", "finished_at": "2026-05-12T09:30:00Z",
             "changes": [{"version": "good1"}]},
            {"number": "1235", "status": "SUCCESS", "buildtype_id": "Wo_Backend_Town",
             "started_at": "2026-05-12T09:35:00Z",
             "changes": [{"version": "bad1"}]},
            {"number": "1236", "status": "SUCCESS", "buildtype_id": "Wo_Backend_Town",
             "started_at": "2026-05-12T09:45:00Z",
             "changes": [{"version": "good2"}]},
        ]
    }

    stats = populate_from_incident(db, _incident(tc=tc))

    # 2 из 3 builds записаны (битый пропущен), service + alert на месте.
    assert calls["n"] == 3
    assert stats["deploys_added"] == 2
    assert stats["services_touched"] == 1
    assert stats["alerts_added"] == 1
    assert db.query(AlertEvent).count() == 1
    # SAVEPOINT на service + 3 build'а + alert = 5 раз.
    # 1 сервис + 3 деплоя + 1 алерт + 1 attach инцидента (kg_incidents,
    # отдельный SAVEPOINT: его сбой не должен отменять записанный алерт).
    assert nested_calls["n"] == 6

    # Главное: Session НЕ отравлена — финальный commit проходит без
    # PendingRollbackError. До фикса упал бы здесь.
    db.commit()

    deploys = recent_deploys_for(
        db, "squad-1", "town-service",
        before=datetime(2026, 5, 12, 10, 0), lookback_minutes=120,
    )
    shas = {d["sha"] for d in deploys}
    assert "good1" in shas and "good2" in shas
    assert "bad1" not in shas


def test_populate_bad_alert_does_not_poison_session(db, monkeypatch):
    """Падение record_alert_event не отравляет Session: записанный ранее
    service сохраняется и commit проходит."""
    import app.knowledge_graph.auto_populator as ap

    def boom(*a, **k):
        raise RuntimeError("simulated IntegrityError on alert")

    monkeypatch.setattr(ap, "record_alert_event", boom)

    stats = populate_from_incident(db, _incident())
    assert stats["services_touched"] == 1
    assert stats["alerts_added"] == 0

    # Session пригодна — service закоммитился.
    db.commit()
    assert db.query(Service).filter(Service.name == "town-service").count() == 1


# ── Атрибуция к k8s-target вместо экспортера ───────────────────────────────


def _exporter_incident(incident_id, labels, namespace="squad-48-kingdom2"):
    return Incident(
        incident_id=incident_id, severity="warning", status="firing",
        summary="x", description="y", namespace=namespace,
        labels={"severity": "warning", **labels},
        annotations={}, starts_at="2026-10-16T10:00:00Z",
    )


def _seed_workload(db, namespace, name):
    from app.knowledge_graph.populator import upsert_service
    upsert_service(db, namespace=namespace, name=name, node_kind="workload")
    db.commit()


def test_pod_alert_via_ksm_lands_on_owning_workload(db):
    """KubePodCrashLooping: `service` = KSM, цель — workload пода. Замер
    16.10.2026: 91 такой алерт за 30 дней лежал на vm-kube-state-metrics."""
    _seed_workload(db, "squad-48-kingdom2", "town-service")
    stats = populate_from_incident(db, _exporter_incident("fp-crash", {
        "alertname": "KubePodCrashLooping",
        "namespace": "squad-48-kingdom2",
        "pod": "town-service-7f8c4b6cdf-h2x9k",
        "container": "town-service",
        "service": "vm-kube-state-metrics",
    }))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-crash").one()
    svc = db.get(Service, alert.service_id)
    assert svc.name == "town-service" and svc.namespace == "squad-48-kingdom2"
    assert alert.raw["source"] == "vm-kube-state-metrics"
    assert alert.raw["attribution"] == "pod"
    assert db.query(Service).filter_by(name="vm-kube-state-metrics").count() == 0
    assert "attribution_unresolved" not in stats
    assert stats.get("kg_incident_id") is not None


def test_deleted_pod_alert_is_not_attributed_to_exporter(db):
    """Workload'а пода в графе нет (под удалён) — алерт без сервиса, а не на
    KSM: guardrail против того же бага для удалённых подов."""
    stats = populate_from_incident(db, _exporter_incident("fp-gone", {
        "alertname": "KubeContainerWaiting",
        "namespace": "squad-48-kingdom2",
        "pod": "old-worker-5d9c7b8f6d-zz9kq",
        "container": "old-worker",
        "service": "vm-kube-state-metrics",
    }))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-gone").one()
    assert alert.service_id is None
    assert alert.raw["source"] == "vm-kube-state-metrics"
    assert alert.raw["attribution"] == "no_target"
    assert stats["attribution_unresolved"] == 1
    assert db.query(Service).count() == 0
    assert db.query(KGIncident).count() == 0


def test_node_alert_via_node_exporter_has_no_service(db):
    """Node* от node-exporter: объект — нода; namespace=monitoring и
    service=vm-node-exporter — метки скрейпа."""
    populate_from_incident(db, _exporter_incident("fp-node", {
        "alertname": "NodeDiskIOSaturation",
        "namespace": "monitoring",
        "pod": "vm-node-exporter-gbn9k",
        "service": "vm-node-exporter",
        "node": "prod-k7",
    }, namespace="monitoring"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-node").one()
    assert alert.service_id is None
    assert alert.raw["node"] == "prod-k7"
    assert alert.raw["source"] == "vm-node-exporter"
    assert alert.raw["attribution"] == "node"
    assert db.query(Service).count() == 0


def test_kube_node_alert_via_ksm_has_no_service(db):
    """KubeNodePressure приходит от KSM с меткой node — тоже нода, не KSM."""
    populate_from_incident(db, _exporter_incident("fp-knode", {
        "alertname": "KubeNodePressure",
        "namespace": "monitoring",
        "node": "dev-32",
        "service": "vm-kube-state-metrics",
    }, namespace="monitoring"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-knode").one()
    assert alert.service_id is None and alert.raw["attribution"] == "node"


def test_ksm_own_pod_is_not_a_target(db):
    """Алерт про под самого KSM — не повод заводить экспортер владельцем."""
    _seed_workload(db, "monitoring", "vm-kube-state-metrics")
    populate_from_incident(db, _exporter_incident("fp-ksm-self", {
        "alertname": "KubePodNotReady",
        "namespace": "monitoring",
        "pod": "vm-kube-state-metrics-7f8c4b6cdf-h2x9k",
        "service": "vm-kube-state-metrics",
    }, namespace="monitoring"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-ksm-self").one()
    assert alert.service_id is None


def test_workload_lookup_error_does_not_poison_store_session(db, monkeypatch):
    """Ошибка проверки workload'а — под SAVEPOINT: алерт всё равно пишется,
    транзакция store-пути не остаётся aborted (ревью Codex)."""
    import app.services.alert_enrichment as ae

    real_query = db.query

    def flaky_query(*args, **kwargs):
        if args and args[0] is Service.id:
            raise RuntimeError("db hiccup")
        return real_query(*args, **kwargs)
    monkeypatch.setattr(db, "query", flaky_query)
    assert ae._workload_exists(db, "squad-48-kingdom2", "town-service") is True
    monkeypatch.setattr(db, "query", real_query)
    populate_from_incident(db, _incident(incident_id="fp-after-hiccup"))
    db.commit()
    assert db.query(AlertEvent).filter_by(fingerprint="fp-after-hiccup").count() == 1


def test_app_alert_without_exporter_keeps_legacy_service(db):
    """Обычный app-алерт: `service` валиден, raw.source не пишется."""
    populate_from_incident(db, _incident(incident_id="fp-app"))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-app").one()
    assert db.get(Service, alert.service_id).name == "town-service"
    assert "source" not in alert.raw


def test_kill_switch_keeps_exporter_attribution_for_pod_alerts(db):
    """Флаг выключен — полностью прежнее поведение, включая pod-алерты."""
    with patch(
        "app.services.alert_enrichment.settings.ATTRIBUTION_RESOLVE_KUBE_TARGET_ENABLED",
        False,
    ):
        populate_from_incident(db, _exporter_incident("fp-ks", {
            "alertname": "KubePodCrashLooping",
            "namespace": "squad-48-kingdom2",
            "pod": "town-service-7f8c4b6cdf-h2x9k",
            "service": "vm-kube-state-metrics",
        }))
    alert = db.query(AlertEvent).filter_by(fingerprint="fp-ks").one()
    assert db.get(Service, alert.service_id).name == "vm-kube-state-metrics"
    assert "source" not in alert.raw
