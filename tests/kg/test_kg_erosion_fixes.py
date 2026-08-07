"""Регрессии «тихой эрозии» KG (ревью 2026-08-07).

Граф не умирал одним махом — он деградировал молча:

  #2  re-fire external probe навсегда оставался «resolved» (ON CONFLICT не
      сбрасывал resolved_at) — health_score / stuck_alerts / RCA слепли.
  #3  kg_log_observations: NULLABLE service_id в UNIQUE-ключе → ON CONFLICT
      не срабатывал для несматченных сервисов (дубли каждый tick); два App-а
      одного сервиса затирали count друг друга.
  #5  kg_deployments: check-then-insert без UNIQUE → дубли из гонки beat-task
      + incident pipeline.
  #6  NATS pub+sub схлопывались в одно ребро, direction флипфлопил.
  #7  kg_k8s_jobs никогда не чистилась — удалённый Job жил вечно.
  #8  metadata_json перезаписывался целиком — источники стирали чужие ключи.
  #9  синки без per-item savepoint: одна битая запись роняла весь tick.
  #10 seq ts_bucket метил строки на окно раньше; ingress:<host> плодился
      per-namespace.

Всё на in-memory SQLite (те же семантики SAVEPOINT/ON CONFLICT, что у PG).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.k8s_jobs_sync import (
    _upsert_k8s_job, cleanup_stale_jobs, sync_all_jobs,
)
from app.knowledge_graph.populator import (
    record_alert_event, record_deployment, upsert_edge, upsert_service,
)
from app.knowledge_graph.queries import nats_impact_for
from app.knowledge_graph.schema import (
    AlertEvent, Deployment, K8sJob, LogObservation, Service, ServiceEdge,
)
from app.knowledge_graph.seq_logs_sync import _upsert_log_obs, _window_bucket


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


# ── #2: re-fire после resolve снова открывает алерт ─────────────────────────


def test_alert_refire_after_resolve_reopens(db):
    """fire → resolve → fire: вторая авария того же host-а снова видна как
    ОТКРЫТЫЙ алерт (resolved_at=NULL, fired_at свежий), а не вечный resolved."""
    svc = upsert_service(db, "prod-shared", "ingress:x.example.com", synthetic=True)
    t0 = datetime(2026, 8, 1, 10, 0)
    t1 = datetime(2026, 8, 7, 12, 0)
    fp = "external_probe:x.example.com"

    ev1 = record_alert_event(db, svc, "ExternalProbeDown", "critical", fp, t0)
    # resolve-путь external_probe_sync проставляет resolved_at.
    ev1.resolved_at = datetime(2026, 8, 1, 11, 0)
    db.commit()

    ev2 = record_alert_event(db, svc, "ExternalProbeDown", "critical", fp, t1)

    assert ev2.id == ev1.id                # тот же fingerprint-row
    assert ev2.resolved_at is None         # алерт снова ОТКРЫТ
    assert ev2.fired_at == t1              # fired_at свежий (новый инцидент)


def test_alert_ongoing_dedup_keeps_original_fired_at(db):
    """Дедуп ещё открытого алерта не сломан: повторный webhook по firing
    алерту сохраняет исходный fired_at (иначе хроника съезжает)."""
    svc = upsert_service(db, "prod-shared", "town-service")
    t0 = datetime(2026, 8, 7, 10, 0)
    t1 = datetime(2026, 8, 7, 10, 30)

    ev1 = record_alert_event(db, svc, "HighLatency", "warning", "fp-1", t0)
    assert ev1.resolved_at is None
    ev2 = record_alert_event(db, svc, "HighLatency", "critical", "fp-1", t1)

    assert ev2.id == ev1.id
    assert ev2.fired_at == t0              # ongoing: оригинальный fired_at
    assert ev2.resolved_at is None
    assert ev2.severity == "critical"      # прежнее поведение сохранено


def test_alert_refire_visible_to_open_alert_queries(db):
    """После re-fire строка снова проходит фильтр resolved_at.is_(None) —
    то, чем её ищут health_score / stuck_alerts / resolve-путь probe."""
    svc = upsert_service(db, "prod-shared", "ingress:y.example.com", synthetic=True)
    fp = "external_probe:y.example.com"
    ev = record_alert_event(
        db, svc, "ExternalProbeDown", "critical", fp, datetime.utcnow(),
    )
    ev.resolved_at = datetime.utcnow()
    db.commit()

    assert (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == fp, AlertEvent.resolved_at.is_(None))
        .one_or_none()
    ) is None

    record_alert_event(
        db, svc, "ExternalProbeDown", "critical", fp, datetime.utcnow(),
    )
    reopened = (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == fp, AlertEvent.resolved_at.is_(None))
        .one_or_none()
    )
    assert reopened is not None


# ── #3: идемпотентность kg_log_observations ─────────────────────────────────


def _log_kwargs(**over):
    base = dict(
        service_id=None,
        ts=datetime(2026, 8, 7, 10, 0),
        level="Error",
        count=5,
        top_message_hash="abc",
        sample_message="boom",
        source="prod",
        namespace=None,
        app_name="GR.WO.Ghost.Service",
    )
    base.update(over)
    return base


def test_unmatched_service_rows_are_idempotent(db):
    """service_id=NULL больше не ломает ON CONFLICT: повторный tick того же
    App-а в том же окне обновляет строку, а не плодит дубль."""
    _upsert_log_obs(db, **_log_kwargs(count=5))
    _upsert_log_obs(db, **_log_kwargs(count=9))
    db.commit()

    rows = db.query(LogObservation).all()
    assert len(rows) == 1
    assert rows[0].count == 9              # свежий пересчёт окна
    assert rows[0].service_id is None


def test_two_apps_same_service_do_not_clobber_counts(db):
    """Два разных Seq App-а одного сервиса — ДВЕ строки; счётчики не
    затирают друг друга, сумма по сервису корректна."""
    svc = upsert_service(db, "prod-shared", "push-service")
    db.flush()
    _upsert_log_obs(db, **_log_kwargs(
        service_id=svc.id, app_name="GR.WO.Push.Service", count=10,
    ))
    _upsert_log_obs(db, **_log_kwargs(
        service_id=svc.id, app_name="GR.WO.Push.Worker", count=7,
    ))
    db.commit()

    rows = db.query(LogObservation).filter_by(service_id=svc.id).all()
    assert len(rows) == 2
    assert sum(r.count for r in rows) == 17


def test_late_service_match_backfills_attribution(db):
    """Сервис появился в KG между тиками → повторный upsert до-проставляет
    service_id; обратной деградации (NULL поверх заполненного) нет."""
    _upsert_log_obs(db, **_log_kwargs(app_name="GR.WO.New.Service"))
    svc = upsert_service(db, "prod-shared", "new-service")
    db.flush()
    _upsert_log_obs(db, **_log_kwargs(
        service_id=svc.id, app_name="GR.WO.New.Service", count=6,
    ))
    # Третий tick снова без матча — атрибуция не стирается.
    _upsert_log_obs(db, **_log_kwargs(
        service_id=None, app_name="GR.WO.New.Service", count=8,
    ))
    db.commit()

    row = db.query(LogObservation).one()
    assert row.service_id == svc.id
    assert row.count == 8


def test_window_bucket_labels_current_window():
    """Бакет — начало ТЕКУЩЕГО окна (от `until`), не предыдущего."""
    until = datetime(2026, 8, 7, 10, 7, 30)
    assert _window_bucket(until, 10) == datetime(2026, 8, 7, 10, 0, 0)
    # Два тика внутри одного окна → один бакет.
    assert _window_bucket(datetime(2026, 8, 7, 10, 9, 59), 10) == \
        _window_bucket(datetime(2026, 8, 7, 10, 0, 1), 10)
    # Следующее окно → следующий бакет.
    assert _window_bucket(datetime(2026, 8, 7, 10, 10, 0), 10) == \
        datetime(2026, 8, 7, 10, 10, 0)


def test_window_bucket_works_for_windows_not_dividing_60():
    """window_minutes=7: раньше replace(minute=...) давал прыгающие границы
    на стыке часов; epoch-выравнивание стабильно."""
    b1 = _window_bucket(datetime(2026, 8, 7, 10, 59, 0), 7)
    b2 = _window_bucket(datetime(2026, 8, 7, 11, 1, 0), 7)
    for b, until in ((b1, datetime(2026, 8, 7, 10, 59)), (b2, datetime(2026, 8, 7, 11, 1))):
        assert b <= until
        assert (until - b) < timedelta(minutes=7)
    # Бакеты выровнены по общей epoch-сетке (кратны 7 минутам).
    assert int(b1.timestamp() if b1.tzinfo else (b1 - datetime(1970, 1, 1)).total_seconds()) % (7 * 60) == 0


# ── #5: kg_deployments — upsert вместо check-then-insert ────────────────────


def test_record_deployment_dedups_same_build(db):
    svc = upsert_service(db, "prod-shared", "town-service")
    t = datetime(2026, 8, 7, 9, 0)
    d1 = record_deployment(db, svc, t, buildtype_id="Bt1", build_number="42")
    d2 = record_deployment(db, svc, t, buildtype_id="Bt1", build_number="42")
    db.commit()

    assert d1.id == d2.id
    assert db.query(Deployment).count() == 1


def test_record_deployment_backfills_sha_only_when_missing(db):
    svc = upsert_service(db, "prod-shared", "town-service")
    t = datetime(2026, 8, 7, 9, 0)
    record_deployment(db, svc, t, buildtype_id="Bt1", build_number="42")
    d2 = record_deployment(
        db, svc, t, sha="abc123", buildtype_id="Bt1", build_number="42",
    )
    assert d2.sha == "abc123"              # бэкфилл пустого sha
    d3 = record_deployment(
        db, svc, t, sha="OTHER", buildtype_id="Bt1", build_number="42",
    )
    assert d3.sha == "abc123"              # существующий sha не перетирается


def test_record_deployment_without_build_info_still_inserts(db):
    """Деплои без build-инфо — отдельные события, дедуп на них не действует."""
    svc = upsert_service(db, "prod-shared", "town-service")
    record_deployment(db, svc, datetime(2026, 8, 7, 9, 0))
    record_deployment(db, svc, datetime(2026, 8, 7, 10, 0))
    assert db.query(Deployment).count() == 2


def test_record_deployment_different_builds_are_separate(db):
    svc = upsert_service(db, "prod-shared", "town-service")
    t = datetime(2026, 8, 7, 9, 0)
    record_deployment(db, svc, t, buildtype_id="Bt1", build_number="42")
    record_deployment(db, svc, t, buildtype_id="Bt1", build_number="43")
    assert db.query(Deployment).count() == 2


# ── #6: NATS direction в идентичности ребра ─────────────────────────────────


def test_nats_impact_shows_both_directions(db):
    """Сервис pub+sub на один subject → nats_impact_for отдаёт обе строки
    со стабильными направлениями (раньше — одна с произвольным)."""
    svc = upsert_service(db, "squad-1", "echo-service")
    subj = upsert_service(db, "nats-subjects", "subject:ping", synthetic=True)
    other = upsert_service(db, "squad-1", "listener-service")
    db.flush()
    upsert_edge(db, svc, subj, kind="uses_nats", direction="pub",
                extras={"direction": "pub"})
    upsert_edge(db, svc, subj, kind="uses_nats", direction="sub",
                extras={"direction": "sub"})
    upsert_edge(db, other, subj, kind="uses_nats", direction="sub",
                extras={"direction": "sub"})
    db.commit()

    impact = nats_impact_for(db, "squad-1", "echo-service")
    assert len(impact) == 2
    assert {e["direction"] for e in impact} == {"pub", "sub"}
    # Ко-сервис считается один раз per subject-запись.
    for e in impact:
        assert e["impact_count"] == 1


def test_upsert_edge_default_direction_still_dedups(db):
    """Рёбра без direction (все остальные kinds) ведут себя как раньше:
    повторный upsert не плодит дубль."""
    a = upsert_service(db, "squad-1", "a")
    b = upsert_service(db, "squad-1", "b")
    db.flush()
    upsert_edge(db, a, b, kind="calls")
    upsert_edge(db, a, b, kind="calls")
    assert db.query(ServiceEdge).count() == 1


# ── #7: bounded cleanup kg_k8s_jobs ──────────────────────────────────────────


def _mk_k8s_job(db, name, *, kind="job", last_seen_days=0):
    node = _upsert_k8s_job(
        db, namespace="prod-shared", name=name, kind=kind,
        fields={"succeeded_count": 1, "failed_count": 0, "active_count": 0},
    )
    if last_seen_days:
        node.last_seen_at = datetime.utcnow() - timedelta(days=last_seen_days)
    return node


def test_cleanup_deletes_stale_jobs(db):
    """Job, не виденный дольше порога, удаляется; свежие — остаются."""
    for i in range(4):
        _mk_k8s_job(db, f"fresh-{i}")
    _mk_k8s_job(db, "deleted-migration", last_seen_days=5)
    db.commit()

    stats = cleanup_stale_jobs(db, kind="job", fetch_count=4)
    db.commit()

    assert stats["deleted"] == 1
    names = {j.name for j in db.query(K8sJob).all()}
    assert "deleted-migration" not in names
    assert len(names) == 4


def test_cleanup_skipped_on_empty_fetch(db):
    """kubectl вернул 0 объектов (сбой неотличим от пустого кластера) —
    ничего не удаляем."""
    _mk_k8s_job(db, "old-one", last_seen_days=5)
    db.commit()

    stats = cleanup_stale_jobs(db, kind="job", fetch_count=0)

    assert stats["skipped"] is True
    assert stats["reason"] == "empty_fetch"
    assert db.query(K8sJob).count() == 1


def test_cleanup_skipped_on_mass_delete(db):
    """Удаление > 25% строк kind-а — симптом сбоя, cleanup пропущен."""
    _mk_k8s_job(db, "fresh-0")
    _mk_k8s_job(db, "old-0", last_seen_days=5)
    _mk_k8s_job(db, "old-1", last_seen_days=5)
    db.commit()

    stats = cleanup_stale_jobs(db, kind="job", fetch_count=1)

    assert stats["skipped"] is True
    assert "delete_pct" in stats["reason"]
    assert db.query(K8sJob).count() == 3


def test_cleanup_respects_kind(db):
    """Чистка job-ов не трогает cronjob-ы (и наоборот)."""
    for i in range(4):
        _mk_k8s_job(db, f"fresh-{i}")
    _mk_k8s_job(db, "old-job", last_seen_days=5)
    _mk_k8s_job(db, "old-cron", kind="cronjob", last_seen_days=5)
    db.commit()

    cleanup_stale_jobs(db, kind="job", fetch_count=4)
    db.commit()

    names = {j.name for j in db.query(K8sJob).all()}
    assert "old-job" not in names
    assert "old-cron" in names


# ── #8: metadata_json merge вместо overwrite ─────────────────────────────────


def test_upsert_service_merges_metadata_sources(db):
    """topology-sync (k8s_service) не стирает ключи auto_populator-а
    (app/component/version) и наоборот — каждый источник владеет своими."""
    upsert_service(db, "prod-shared", "town-service",
                   metadata={"app": "town", "version": "1.2.3"})
    upsert_service(db, "prod-shared", "town-service",
                   metadata={"k8s_service": {"service_type": "ClusterIP"}})
    svc = db.query(Service).filter_by(name="town-service").one()

    assert svc.metadata_json["app"] == "town"
    assert svc.metadata_json["version"] == "1.2.3"
    assert svc.metadata_json["k8s_service"]["service_type"] == "ClusterIP"


def test_upsert_service_own_keys_are_updated(db):
    """Свои ключи источник обновляет (merge — не freeze)."""
    upsert_service(db, "prod-shared", "town-service",
                   metadata={"k8s_service": {"service_type": "ClusterIP"}})
    upsert_service(db, "prod-shared", "town-service",
                   metadata={"k8s_service": {"service_type": "NodePort"}})
    svc = db.query(Service).filter_by(name="town-service").one()
    assert svc.metadata_json["k8s_service"]["service_type"] == "NodePort"


# ── #9: per-item изоляция синков ─────────────────────────────────────────────


def test_sync_all_jobs_survives_poisoned_item(db):
    """Одна битая запись не роняет tick: соседи закоммичены, errors=1."""
    jobs = [
        {"metadata": {"name": "good-1", "namespace": "ns"},
         "spec": {"template": {"metadata": {"labels": {}}, "spec": {}}},
         "status": {"succeeded": 1}},
        {"metadata": {"name": "poison", "namespace": "ns"},
         "spec": {"template": {"metadata": {"labels": {}}, "spec": {}}},
         "status": {"succeeded": 1}},
        {"metadata": {"name": "good-2", "namespace": "ns"},
         "spec": {"template": {"metadata": {"labels": {}}, "spec": {}}},
         "status": {"succeeded": 1}},
    ]

    import app.knowledge_graph.k8s_jobs_sync as mod
    real_upsert = mod._upsert_k8s_job

    def poisoned_upsert(db_, *, namespace, name, kind, fields, metadata=None):
        if name == "poison":
            raise ValueError("simulated DataError")
        return real_upsert(
            db_, namespace=namespace, name=name, kind=kind,
            fields=fields, metadata=metadata,
        )

    with patch.object(mod, "_kubectl_get_all", return_value=jobs), \
         patch.object(mod, "_upsert_k8s_job", side_effect=poisoned_upsert):
        stats = sync_all_jobs(db)

    assert stats["errors"] == 1
    assert stats["nodes_upserted"] == 2
    names = {j.name for j in db.query(K8sJob).all()}
    assert names == {"good-1", "good-2"}


def test_ingress_sync_survives_poisoned_route(db):
    """k8s_ingress_sync: битый route откатывается savepoint-ом, остальные
    routes и терминальный commit живут."""
    import app.knowledge_graph.k8s_ingress_sync as mod

    upsert_service(db, "ns", "backend-a")
    upsert_service(db, "ns", "backend-b")
    db.commit()

    ing = {
        "metadata": {"name": "main", "namespace": "ns"},
        "spec": {"rules": [
            {"host": "a.example.com", "http": {"paths": [
                {"path": "/", "backend": {"service": {"name": "backend-a"}}},
            ]}},
            {"host": "poison.example.com", "http": {"paths": [
                {"path": "/", "backend": {"service": {"name": "backend-b"}}},
            ]}},
        ]},
    }

    real_upsert_edge = mod.upsert_edge

    def poisoned_edge(db_, *, src, dst, kind, **kw):
        if src.name == "ingress:poison.example.com":
            raise ValueError("simulated DataError")
        return real_upsert_edge(db_, src=src, dst=dst, kind=kind, **kw)

    with patch.object(mod, "_kubectl_get_ingresses_all", return_value=[ing]), \
         patch.object(mod, "upsert_edge", side_effect=poisoned_edge):
        stats = mod.sync_all_ingresses(db)

    assert stats["errors"] == 1
    assert stats["edges_created"] == 1
    edges = db.query(ServiceEdge).all()
    assert len(edges) == 1
    assert edges[0].dst.name == "backend-a"
    # Частичная запись битого route (узел ingress:poison...) откатана.
    assert (
        db.query(Service).filter_by(name="ingress:poison.example.com").count()
        == 0
    )


# ── #10d: ingress:<host> — один узел на hostname ─────────────────────────────


def test_ingress_host_node_not_duplicated_across_namespaces(db):
    """Один host в двух namespace-ах → ОДИН ingress:<host>-узел (не два,
    деливших один external_probe-fingerprint)."""
    import app.knowledge_graph.k8s_ingress_sync as mod

    upsert_service(db, "ns-a", "backend-a")
    upsert_service(db, "ns-b", "backend-b")
    db.commit()

    def _ing(ns, backend):
        return {
            "metadata": {"name": f"ing-{ns}", "namespace": ns},
            "spec": {"rules": [
                {"host": "shared.example.com", "http": {"paths": [
                    {"path": "/", "backend": {"service": {"name": backend}}},
                ]}},
            ]},
        }

    with patch.object(
        mod, "_kubectl_get_ingresses_all",
        return_value=[_ing("ns-b", "backend-b"), _ing("ns-a", "backend-a")],
    ):
        stats = mod.sync_all_ingresses(db)

    nodes = (
        db.query(Service)
        .filter(Service.name == "ingress:shared.example.com")
        .all()
    )
    assert len(nodes) == 1
    # Оба edge ведут из одного узла.
    edges = db.query(ServiceEdge).filter(ServiceEdge.src_id == nodes[0].id).all()
    assert {e.dst.name for e in edges} == {"backend-a", "backend-b"}
    assert stats["edges_created"] == 2


def test_ingress_host_node_reuses_existing_canonical(db):
    """Узел host-а уже существует (создан ранее в другом ns) —
    переиспользуем его, а не плодим копию в ns текущего Ingress-а."""
    import app.knowledge_graph.k8s_ingress_sync as mod

    existing = upsert_service(
        db, "old-ns", "ingress:host.example.com",
        team_owner="external", synthetic=True,
    )
    upsert_service(db, "new-ns", "backend")
    db.commit()

    ing = {
        "metadata": {"name": "ing", "namespace": "new-ns"},
        "spec": {"rules": [
            {"host": "host.example.com", "http": {"paths": [
                {"path": "/", "backend": {"service": {"name": "backend"}}},
            ]}},
        ]},
    }
    with patch.object(mod, "_kubectl_get_ingresses_all", return_value=[ing]):
        mod.sync_all_ingresses(db)

    nodes = (
        db.query(Service)
        .filter(Service.name == "ingress:host.example.com")
        .all()
    )
    assert len(nodes) == 1
    assert nodes[0].id == existing.id
    assert nodes[0].namespace == "old-ns"
