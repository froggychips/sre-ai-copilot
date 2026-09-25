"""Контекст инцидента из графа на момент времени: один сборщик для правил,
модели и датасета (app/context/kg_incident_context.py)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.context import kg_incident_context as kgi
from app.database import Base
from app.knowledge_graph.schema import (AlertEvent, Deployment, K8sJob,
                                        K8sJobRun, KGIncident,
                                        KGRemediationEvent, LogObservation,
                                        PodEvent, Service)
from app.models.incident import Incident

AS_OF = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
N = AS_OF.replace(tzinfo=None)          # граф хранит naive UTC
M = timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _cli_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        _seed(s)
        yield s
    finally:
        s.close()
        engine.dispose()


def _svc(s, ns, name):
    svc = Service(namespace=ns, name=name)
    s.add(svc)
    s.flush()
    return svc


def _seed(s):
    bravo = _svc(s, "squad-9-kingdom2", "bravo-service")
    alpha = _svc(s, "squad-9-shared", "alpha-service")
    other = _svc(s, "squad-10-shared", "other-service")
    s.add_all([
        # CrashLoop bravo: начался до as_of, дописывался ПОСЛЕ — count неизвестен.
        PodEvent(service_id=bravo.id, event_uid="u1", namespace="squad-9-kingdom2",
                 pod_name="bravo-service-7d9f8b6c5d-x2k4q", reason="BackOff", type="Warning",
                 message="Back-off restarting failed container token=abc123",
                 first_seen=N - 50 * M, last_seen=N + 30 * M, count=40),
        # Закончился до as_of — count честный.
        PodEvent(service_id=bravo.id, event_uid="u2", namespace="squad-9-kingdom2",
                 pod_name="bravo-service-7d9f8b6c5d-x2k4q", reason="OOMKilling", type="Warning",
                 message="Memory cgroup out of memory", first_seen=N - 40 * M,
                 last_seen=N - 20 * M, count=3),
        # Начался после as_of — будущее, не попадает.
        PodEvent(service_id=alpha.id, event_uid="u3", namespace="squad-9-shared",
                 pod_name="alpha-service-5c7b9d8f6g-k2m4n", reason="Unhealthy", type="Warning",
                 message="Readiness probe failed", first_seen=N + 5 * M, last_seen=N + 6 * M, count=2),
        # Другой сквад — не наш скоуп.
        PodEvent(service_id=other.id, event_uid="u4", namespace="squad-10-shared",
                 pod_name="other-service-7d9f8b6c5d-x2k4q", reason="BackOff", type="Warning",
                 message="x", first_seen=N - 10 * M, last_seen=N - 5 * M, count=9),
        Deployment(service_id=bravo.id, buildtype_id="Wo_Backend_BuildAndUpdate",
                   build_number="512", status="SUCCESS", started_at=N - 90 * M,
                   finished_at=N - 80 * M, extras={"namespace_scope": False}),
        Deployment(service_id=alpha.id, buildtype_id="Wo_StaticsNewCluster_Gd_Rebuild",
                   status="SUCCESS", started_at=N - 30 * M, finished_at=N - 29 * M),
        Deployment(service_id=alpha.id, buildtype_id="Wo_StaticsNewCluster_Gd_Rebuild",
                   status="SUCCESS", started_at=N - 60 * M, finished_at=N - 59 * M),
        Deployment(service_id=alpha.id, buildtype_id="k8s_rollout",
                   status="SUCCESS", started_at=N - 15 * M, finished_at=N - 14 * M),
        K8sJob(namespace="squad-9-kingdom2", name="bravo-db-migrate", kind="Job",
               failed_count=2, succeeded_count=0, last_pod_exit_code=1,
               start_time=N - 70 * M, last_seen_at=N - 2 * M),
        K8sJob(namespace="squad-9-shared", name="chat-db-migrate", kind="Job",
               failed_count=0, succeeded_count=1, start_time=N - 70 * M,
               completion_time=N - 69 * M, last_seen_at=N + 3 * 60 * M),
        # История Job-ов (#454): упал до as_of; успех того же Job-а — после.
        K8sJobRun(namespace="squad-9-kingdom2", name="bravo-db-migrate", failed_count=2,
                  succeeded_count=0, active_count=0, last_pod_exit_code=1,
                  condition_type="Failed", condition_reason="BackoffLimitExceeded",
                  start_time=N - 70 * M, observed_at=N - 60 * M, disappeared=False),
        K8sJobRun(namespace="squad-9-kingdom2", name="bravo-db-migrate", failed_count=2,
                  succeeded_count=1, active_count=0, condition_type="Complete",
                  start_time=N + 5 * M, observed_at=N + 20 * M, disappeared=False),
        KGIncident(incident_key="squad-9-shared/alpha-service@1", namespace="squad-9-shared",
                   service_name="alpha-service", status="resolved", severity="warning",
                   opened_at=N - 2 * 24 * 60 * M, last_alert_at=N - 2 * 24 * 60 * M,
                   resolved_at=N - 2 * 24 * 60 * M + 20 * M, resolve_reason="all_alerts_resolved",
                   alert_count=1, alertnames=["KubeDeploymentGenerationMismatch"], noise=True,
                   extras={"noise_fingerprints": {"fp-gm": ["controller_lag_rancher_churn"]}}),
        KGIncident(incident_key="squad-9-shared/alpha-service@2", namespace="squad-9-shared",
                   service_name="alpha-service", status="open", severity="warning",
                   opened_at=N - 20 * M, last_alert_at=N - 20 * M, alert_count=1,
                   alertnames=["KubeDeploymentGenerationMismatch"], noise=True,
                   extras={"noise_fingerprints": {"fp-gm": ["controller_lag_rancher_churn"]}}),
        AlertEvent(service_id=alpha.id, alertname="KubeDeploymentGenerationMismatch",
                   severity="warning", fingerprint="fp-gm", fired_at=N - 20 * M,
                   incident_id="squad-9-shared/alpha-service@2"),
        AlertEvent(service_id=bravo.id, alertname="KubePodCrashLooping", severity="critical",
                   fingerprint="fp-cl", fired_at=N - 30 * M, resolved_at=N + 40 * M),
        # Прошлый разбор медика — закончен до as_of: история.
        KGRemediationEvent(actor="squad-medic", run_id="r1", namespace="squad-9-shared",
                           namespaces=["squad-9-shared"], started_at=N - 5 * 60 * M,
                           finished_at=N - 5 * 60 * M + 3 * M, outcome="partial", fixed=True,
                           still_unhealthy=True, applied=["rollout restart bravo-service"],
                           summary="bravo-service в CrashLoopBackOff",
                           root_cause="вывод прошлого разбора: сломан конфиг"),
        # Текущий разбор (as_of = его начало): наблюдения сохранены при приёме.
        KGRemediationEvent(id=77, actor="squad-medic", run_id="r2", namespace="squad-9-shared",
                           namespaces=["squad-9-shared"], started_at=N, finished_at=N + 4 * M,
                           outcome="partial", fixed=True, still_unhealthy=True,
                           applied=["grants: shared"], summary="прочее",
                           root_cause="ответ кейса: dirty-миграция bravo-db",
                           observations={"schema": "medic_obs/v1", "provenance": "squad-medic",
                                         "observed_at": AS_OF.isoformat(),
                                         "namespace": "squad-9-shared", "namespaces": [],
                                         "facts": ["schema_migrations: dirty=true"]}),
        LogObservation(service_id=bravo.id, namespace="squad-9-kingdom2", app_name="bravo-service",
                       level="Error", count=17, sample_message="column \"x\" does not exist",
                       ts=N - 10 * M, source="seq", top_message_hash="h1"),
    ])
    s.commit()


def _scope(**over):
    base = dict(namespace="squad-9-shared", service="alpha-service",
                alertname="KubeDeploymentGenerationMismatch", as_of=AS_OF)
    base.update(over)
    return kgi.KGScope(**base)


def _kgc(db, **over):
    return kgi.fetch_kg_incident_context(kgi.SessionReader(db), _scope(**over))


# --- чтение: скоуп сквада и point-in-time ------------------------------------


def test_squad_scope_and_point_in_time_clipping(db):
    kgc = _kgc(db)
    assert kgc["schema"] == kgi.SCHEMA and kgc["ns_scope"] == "squad-9-%"
    assert set(kgc["namespaces"]) == {"squad-9-shared", "squad-9-kingdom2"}
    by_reason = {e["reason"]: e for e in kgc["pod_events"]}
    assert set(by_reason) == {"BackOff", "OOMKilling"}   # будущее и чужой сквад — нет
    backoff = by_reason["BackOff"]
    # Дописанный после as_of агрегат: last_seen обрезан, count неизвестен.
    assert backoff["last_seen"] == AS_OF.isoformat() and backoff["count"] is None
    assert by_reason["OOMKilling"]["count"] == 3
    assert "abc123" not in json.dumps(kgc)                # redact_pii
    # Резолв алерта после as_of — будущее.
    cl = next(a for a in kgc["alerts"] if a["alertname"] == "KubePodCrashLooping")
    assert cl["resolved_at"] is None


def test_deploys_split_code_rollout_statics(db):
    d = _kgc(db)["deployments"]
    assert [x["service"] for x in d["code"]] == ["bravo-service"]
    assert d["code"][0]["attribution_scope"] == "service"
    assert [x["kind"] for x in d["rollouts"]] == ["rollout"]
    assert d["statics_count"] == 2


def test_jobs_come_from_history_at_as_of(db):
    jobs = {j["name"]: j for j in _kgc(db)["jobs"]}
    # Успех после as_of — будущее: на момент инцидента migrate-job упал.
    assert jobs["bravo-db-migrate"]["status"] == "failed"
    assert jobs["bravo-db-migrate"]["source"] == "kg_k8s_job_runs"
    assert "chat-db-migrate" not in jobs      # в истории его нет — снимок не подмешан


class _NoHistory(kgi.SessionReader):
    def has_column(self, table, column):
        return False if table == "kg_k8s_job_runs" else super().has_column(table, column)


def test_jobs_snapshot_is_flagged_when_updated_after_as_of(db):
    kgc = kgi.fetch_kg_incident_context(_NoHistory(db), _scope())
    jobs = {j["name"]: j for j in kgc["jobs"]}
    assert jobs["bravo-db-migrate"]["failed"] == 2 and jobs["bravo-db-migrate"]["migrate"]
    assert not jobs["bravo-db-migrate"]["state_after_as_of"]
    assert jobs["chat-db-migrate"]["state_after_as_of"]
    # Счётчики строки, обновлённой после инцидента, наружу не отдаются.
    assert jobs["chat-db-migrate"]["failed"] is None and jobs["chat-db-migrate"]["exit_code"] is None


def test_post_as_of_job_state_never_reaches_text_or_prompt():
    kgc = {"schema": kgi.SCHEMA, "as_of": AS_OF.isoformat(), "sources": {},
           "jobs": [{"namespace": "n", "name": "late-migrate", "failed": 3, "migrate": True,
                     "state_after_as_of": True}]}
    ctx = kgi.apply_kg_context({"service": "x", "source_status": {}}, kgc)
    assert "late-migrate" not in (ctx.get("k8s_summary") or "")
    assert "late-migrate" not in kgi.kg_context_prompt(kgc)


def test_noise_alerts_are_marked(db):
    gm = next(a for a in _kgc(db)["alerts"] if a["alertname"] == "KubeDeploymentGenerationMismatch")
    assert gm["noise_kinds"] == ["controller_lag_rancher_churn"]


def test_history_excludes_own_run_and_conclusions_on_demand(db):
    prod = _kgc(db)
    assert {h["event_id"] for h in prod["remediation_history"]} == {
        e.id for e in db.query(KGRemediationEvent).filter(KGRemediationEvent.run_id == "r1")}
    assert prod["remediation_history"][0]["root_cause"].startswith("вывод прошлого")
    assert prod["remediation_history"][0]["fixed_semantics"] == "applied_something"
    ds = _kgc(db, with_conclusions=False, exclude_remediation_ids=(77,))
    assert all("root_cause" not in h for h in ds["remediation_history"])
    assert "ответ кейса" not in json.dumps(ds, ensure_ascii=False)
    # Наблюдения собственного разбора остаются: сняты с живого стенда на as_of.
    facts = [f for o in ds["medic_observations"] for f in o["facts"]]
    assert "schema_migrations: dirty=true" in facts
    assert prod["incident_history"][0]["noise"] >= 1


def test_observations_fall_back_to_extractor_for_old_events(db):
    db.add(KGRemediationEvent(actor="squad-medic", run_id="r0", namespace="squad-9-shared",
                              started_at=N - 30 * M, finished_at=N - 28 * M, outcome="partial",
                              summary="поды в ImagePullBackOff", root_cause="секрет"))
    db.commit()
    facts = [f for o in _kgc(db)["medic_observations"] for f in o["facts"]]
    assert "состояние подов: ImagePullBackOff" in facts


def test_failed_source_is_unknown_not_absent(db, monkeypatch):
    from sqlalchemy import text

    def bad_sql(reader, *_a, **_k):
        # Настоящая ошибка запроса: в PostgreSQL она оставила бы транзакцию
        # aborted — источник обязан быть под savepoint.
        reader.db.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(kgi, "_SOURCES", tuple(
        (n, bad_sql if n in ("kg_k8s_jobs", "kg_log_observations") else f)
        for n, f in kgi._SOURCES))
    kgc = _kgc(db)
    assert kgc["sources"]["kg_k8s_jobs"]["status"] == "failed"
    assert kgc["pod_events"] and kgc["remediation_history"]   # остальные на месте
    db.commit()                                              # сессия цела
    ctx = kgi.apply_kg_context({"service": "bravo-service", "source_status": {}}, kgc)
    assert ctx["source_status"]["kg_jobs"].startswith("kg_k8s_jobs недоступен")
    assert ctx["source_status"]["logs_summary"].startswith("kg_log_observations недоступен")
    assert "kg_k8s_jobs" in kgi.kg_context_prompt(kgc)


# --- раскладка в ctx и текст модели ------------------------------------------


def test_apply_feeds_rules_and_keeps_foreign_text_out(db):
    kgc = _kgc(db)
    ctx = kgi.apply_kg_context({"service": "alpha-service", "alertname": "X",
                                "source_status": {}}, kgc)
    # Строка медика «dirty=true» — не событие пода: только текст [squad-medic].
    assert {e["source"] for e in ctx["k8s_events"]} == {"kg_pod_events"}
    # Текст — только про target (alpha): BackOff bravo туда не попадает.
    assert "Back-off" not in (ctx.get("k8s_summary") or "")
    assert "[kg_k8s_jobs]" in ctx["k8s_summary"] and "bravo-db-migrate" in ctx["k8s_summary"]
    assert "[squad-medic]" in ctx["k8s_summary"]
    assert [d["name"] for d in ctx["recent_deployments"]] == ["bravo-service"]
    assert ctx["recent_deployments"][0]["attribution_scope"] == "namespace"
    assert ctx["source_status"]["k8s_events"].startswith("partial")
    assert "[kg_log_observations]" in ctx["logs_summary"]
    no_medic = kgi.apply_kg_context({"service": "alpha-service", "source_status": {}},
                                    kgc, include_medic=False)
    assert "squad-medic" not in (no_medic.get("k8s_summary") or "")


def test_noise_alert_target_is_replaced_by_broken_workload(db):
    kgc = _kgc(db)
    ctx = kgi.apply_kg_context({"service": "alpha-service",
                                "alertname": "KubeDeploymentGenerationMismatch",
                                "source_status": {}}, kgc)
    assert ctx["service"] == "bravo-service" and ctx["service_from"] == "kg_targets"
    kept = kgi.apply_kg_context({"service": "alpha-service", "alertname": "KubePodCrashLooping",
                                 "source_status": {}}, kgc)
    assert kept["service"] == "alpha-service"


def test_migration_rule_sees_failed_job_from_graph(db):
    from app.diagnostics import default_engine

    kgc = _kgc(db)
    ctx = kgi.apply_kg_context({"service": "bravo-service", "namespace": "squad-9-kingdom2",
                                "alertname": "KubePodCrashLooping", "source_status": {}}, kgc)
    store = default_engine.run(ctx)
    fact = next(f for f in store.facts if f.kind == "migration_failed" and f.observed)
    assert fact.evidence["job"] == "bravo-db-migrate"


def test_prompt_has_sources_and_no_conclusions_when_asked(db):
    text = kgi.kg_context_prompt(_kgc(db, with_conclusions=False))
    for marker in ("[kg_pod_events]", "[kg_k8s_jobs]", "[kg_deployments]", "[squad-medic]",
                   "[kg_remediation_events]", "[kg_incidents]"):
        assert marker in text, marker
    assert "its conclusion" not in text
    assert "its conclusion" in kgi.kg_context_prompt(_kgc(db))
    assert kgi.kg_context_prompt(None) == ""


# --- один путь для прода и датасета --------------------------------------------


def _incident():
    return Incident(incident_id="i1", severity="warning", status="firing", summary="s",
                    namespace="squad-9-shared",
                    labels={"alertname": "KubePodCrashLooping", "namespace": "squad-9-shared",
                            "service": "alpha-service"},
                    annotations={}, starts_at=AS_OF.isoformat())


def test_prod_and_dataset_build_the_same_ctx(db, monkeypatch):
    """Прод собирает контекст сессией, датасет хранит его JSON-ом в кейсе —
    правила получают одно и то же."""
    from app.diagnostics import incident_ctx

    monkeypatch.setattr(incident_ctx, "nearby_alerts", lambda *_a, **_k: [])
    prod = incident_ctx.build_diagnostics_ctx(_incident(), "", kg_session=db)
    stored = json.loads(json.dumps(kgi.fetch_kg_incident_context(
        kgi.SessionReader(db), kgi.KGScope(namespace="squad-9-shared", service="alpha-service",
                                           alertname="KubePodCrashLooping", as_of=AS_OF))))
    ds = incident_ctx.build_diagnostics_ctx(_incident(), "", kg_session=None, kg_context=stored)
    for key in ("service", "k8s_events", "k8s_summary", "logs_summary", "recent_deployments",
                "kg_jobs", "kg_squad_alerts", kgi.CTX_KEY):
        assert prod.get(key) == ds.get(key), key
    assert prod[kgi.CTX_KEY]["pod_events"]


def test_psql_reader_runs_the_same_select_as_sql():
    sent = []

    def fake_psql(sql):
        sent.append(sql)
        if "information_schema" in sql:
            return '{"column_name": "observations"}\n'
        return '{"namespace": "squad-9-shared"}\n'

    r = kgi.PsqlReader(fake_psql)
    from sqlalchemy import select
    stmt = select(Service.namespace).where(Service.namespace.like("squad-9-%"))
    assert r.rows(stmt) == [{"namespace": "squad-9-shared"}]
    assert kgi.render_sql(stmt) in sent[0] and "READ ONLY" in sent[0]
    assert r.has_column("kg_remediation_events", "observations")
    assert not r.has_column("kg_remediation_events", "nope")


def test_service_pod_events_in_embed_shape(db):
    kgc = _kgc(db)
    rows = kgi.service_pod_events(kgc, "squad-9-kingdom2", "bravo-service", around=AS_OF)
    assert rows and {"reason", "pod_name", "first_seen", "last_seen", "count",
                     "minutes_before", "minutes_since_last", "message"} <= set(rows[0])
    assert kgi.service_pod_events(None, "n", "s", around=AS_OF) is None


# --- target: сломанный workload, а не ближайший инцидент -----------------------

_T0 = "2026-09-20T10:00:00+00:00"


def _tk(pod_events=(), alerts=(), code=(), rollouts=()):
    return {"as_of": _T0, "pod_events": list(pod_events), "alerts": list(alerts),
            "deployments": {"code": list(code), "rollouts": list(rollouts), "statics_count": 0}}


@pytest.mark.parametrize("pod,workload", [
    ("bravo-service-7d9f8b6c5d-x2k4q", "bravo-service"),
    ("town-db-0", "town-db"),
    ("migrate-job-7x2kq", "migrate-job"),
    ("backup-29812217-q5z7m", "backup"),
    ("plain", "plain"),
])
def test_workload_of_strips_controller_suffixes(pod, workload):
    assert kgi.workload_of(pod) == workload


def test_broken_workload_wins_over_noise_incident_service():
    kgc = _tk(
        pod_events=[{"namespace": "squad-9-kingdom2", "pod": "bravo-service-7d9f8b6c5d-x2k4q",
                     "type": "Warning", "reason": "BackOff", "count": 12,
                     "last_seen": "2026-09-20T09:55:00+00:00"},
                    {"namespace": "squad-9-shared", "pod": "alpha-service-5c7b9d8f6g-k2m4n",
                     "type": "Normal", "reason": "Pulled", "count": 1,
                     "last_seen": "2026-09-20T09:58:00+00:00"}],
        alerts=[{"namespace": "squad-9-shared", "service": "alpha-service",
                 "alertname": "KubeDeploymentGenerationMismatch",
                 "fired_at": "2026-09-20T09:50:00+00:00"}])
    targets = kgi.select_targets(kgc)
    assert [t["workload"] for t in targets] == ["bravo-service"]
    assert targets[0]["namespace"] == "squad-9-kingdom2"


def test_generation_mismatch_counts_only_with_deploy():
    alert = {"namespace": "squad-9-shared", "service": "alpha-service",
             "alertname": "KubeDeploymentGenerationMismatch", "fired_at": "2026-09-20T09:50:00+00:00"}
    assert kgi.select_targets(_tk(alerts=[alert])) == []
    with_deploy = _tk(alerts=[alert], code=[{"namespace": "squad-9-shared",
                                              "service": "alpha-service"}])
    assert [t["workload"] for t in kgi.select_targets(with_deploy)] == ["alpha-service"]
    marked = dict(alert, noise_kinds=["controller_lag_rancher_churn"])
    assert kgi.select_targets(_tk(alerts=[marked], code=[{"namespace": "squad-9-shared",
                                                           "service": "alpha-service"}])) == []


def test_closer_events_rank_higher():
    kgc = _tk(pod_events=[
        {"namespace": "n", "pod": "old-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": "2026-09-20T08:00:00+00:00"},
        {"namespace": "n", "pod": "new-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": "2026-09-20T09:58:00+00:00"}])
    assert kgi.select_targets(kgc)[0]["workload"] == "new-svc"


def test_prior_actions_only_boost_known_candidates():
    kgc = _tk(pod_events=[
        {"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 4,
         "last_seen": _T0},
        {"namespace": "n", "pod": "b-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": _T0}])
    targets = kgi.select_targets(kgc, '[["rollout restart a-svc", "restart ghost-svc"]]')
    assert targets[0]["workload"] == "a-svc" and "medic_applied" in targets[0]["sources"]
    assert "ghost-svc" not in {t["workload"] for t in targets}


def test_probe_failures_during_rollout_do_not_pick_target():
    kgc = _tk(pod_events=[
        {"namespace": "n", "pod": "town-grainhost-7d9f8b6c5d-x2k4q", "reason": "Unhealthy",
         "count": 20, "last_seen": _T0},
        {"namespace": "n", "pod": "map-service-7d9f8b6c5d-x2k4q", "reason": "BackOff",
         "count": 2, "last_seen": _T0}],
        rollouts=[{"namespace": "n", "service": "town-grainhost"}])
    assert [t["workload"] for t in kgi.select_targets(kgc)] == ["map-service"]


def test_probe_failures_are_soft_without_rollout():
    kgc = _tk(pod_events=[
        {"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q", "reason": "Unhealthy", "count": 10,
         "last_seen": _T0},
        {"namespace": "n", "pod": "b-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 3,
         "last_seen": _T0}])
    assert kgi.select_targets(kgc)[0]["workload"] == "b-svc"


def test_metric_source_and_resolved_alerts_are_never_targets():
    kgc = _tk(alerts=[
        {"namespace": "n", "service": "vm-kube-state-metrics", "alertname": "KubeContainerWaiting",
         "fired_at": _T0},
        {"namespace": "n", "service": "healed-svc", "alertname": "KubePodCrashLooping",
         "fired_at": _T0, "resolved_at": _T0}])
    assert kgi.select_targets(kgc) == []


def test_boost_ignores_probe_only_candidates():
    kgc = _tk(pod_events=[{"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q",
                           "reason": "Unhealthy", "count": 5, "last_seen": _T0}])
    t = kgi.select_targets(kgc, '[["rollout restart a-svc"]]')[0]
    assert "medic_applied" not in t["sources"]
