"""Снимок контекста инцидента: схема, PII, лимит размера, живучесть, датасет."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.context.incident_snapshot import (MAX_SNAPSHOT_BYTES, SNAPSHOT_SCHEMA,
                                           build_context_snapshot)
from app.database import Base, IncidentRecord
from app.diagnostics.facts import Fact, FactStore
from app.remediation import attempts as _attempts  # noqa: F401 — регистрирует таблицу
from app.remediation.models import RemediationDecision  # noqa: F401 — то же

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_rca_dataset.py"
_spec = importlib.util.spec_from_file_location("live_rca_dataset_snap", _PATH)
assert _spec and _spec.loader
lrd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lrd)


def _ctx(**over):
    ctx = {
        "incident": {"labels": {"alertname": "KubePodCrashLooping", "namespace": "squad-1",
                                "pod": "town-service-abc", "prometheus": "monitoring/vm",
                                "severity": "critical"},
                     "starts_at": "2026-09-24T10:00:00Z"},
        "namespace": "squad-1",
        "service": "town-service",
        "pod": "town-service-abc",
        "alertname": "KubePodCrashLooping",
        "description": "Pod is crash looping",
        "analyzer_summary": "LLM проза — в снимок не входит",
        "k8s_pod_state": {"town-service-abc": {"reason": "Error", "exit_code": 139,
                                               "message": "segfault"}},
        "k8s_events": [{"type": "Warning", "reason": "BackOff",
                        "message": "Back-off restarting failed container", "count": 12}],
        "recent_deployments": [{"name": "town-service", "ts": "2026-09-24T09:50:00Z"}],
        "logs_summary": "line1\nline2\nFATAL: boom",
        "source_status": {"upstream_alerts": "failed: OperationalError"},
        "incident_starts_at": datetime(2026, 9, 24, 10, 0, 0),
    }
    ctx.update(over)
    return ctx


def _facts():
    return FactStore([
        Fact(kind="process_crash", observed=True, confidence=0.9,
             evidence={"exit_code": 139, "signal": "SIGSEGV"}, source_rule="process_crash"),
        Fact(kind="oom_killed", observed=False, confidence=0.9, verdict="absent"),
    ])


def test_schema_and_sections():
    snap = build_context_snapshot(_ctx(), _facts())
    assert snap["schema"] == SNAPSHOT_SCHEMA
    assert snap["alert"]["alertname"] == "KubePodCrashLooping"
    # Шумовые метки отброшены, смысловые — на месте.
    assert "prometheus" not in snap["alert"]["labels"]
    assert snap["alert"]["labels"]["severity"] == "critical"
    assert [f["kind"] for f in snap["facts"]] == ["process_crash", "oom_killed"]
    assert snap["facts"][1]["verdict"] == "absent"
    assert snap["k8s_pod_state"]["town-service-abc"]["exit_code"] == 139
    assert snap["source_status"] == {"upstream_alerts": "failed: OperationalError"}
    assert snap["alert"]["starts_at"] == "2026-09-24T10:00:00"
    assert snap["truncated"] == []
    assert snap["bytes"] == len(json.dumps(
        {k: v for k, v in snap.items() if k != "bytes"}, ensure_ascii=False, default=str
    ).encode("utf-8"))


def test_llm_prose_is_not_part_of_snapshot():
    blob = json.dumps(build_context_snapshot(_ctx(), _facts()), ensure_ascii=False)
    assert "LLM проза" not in blob


def test_pii_is_redacted_everywhere():
    ctx = _ctx(
        logs_summary="connecting postgres://app:hunter2@db:5432/x password=s3cr3t",
        k8s_events=[{"type": "Warning", "reason": "Failed",
                     "message": "notify admin@example.com Bearer abc.def.ghi"}],
        description="user bob@example.org hit it",
    )
    blob = json.dumps(build_context_snapshot(ctx, _facts()), ensure_ascii=False)
    for secret in ("hunter2", "s3cr3t", "admin@example.com", "bob@example.org"):
        assert secret not in blob


def test_size_limit_trims_by_priority_and_marks_it():
    ctx = _ctx(
        logs_summary="x" * 200_000,
        k8s_events=[{"type": "Warning", "reason": f"R{i}", "message": "m" * 250, "count": i}
                    for i in range(500)],
        upstream_alerts=[{"alertname": f"A{i}", "text": "t" * 250} for i in range(300)],
    )
    snap = build_context_snapshot(ctx, _facts())
    assert snap["bytes"] <= MAX_SNAPSHOT_BYTES
    assert snap["truncated"], "срезанное должно быть перечислено"
    # Неприкасаемые секции живы.
    assert snap["alert"]["alertname"] == "KubePodCrashLooping"
    assert len(snap["facts"]) == 2
    assert snap["source_status"]
    # Логи — хвост, а не голова.
    if snap["logs_summary"]:
        assert snap["logs_summary"].startswith("…")


def test_tiny_budget_drops_fact_evidence_last():
    # Бюджет — ровно под снимок без режущихся секций и без evidence фактов:
    # срезаться должно всё режущееся, потом evidence, но не сами вердикты.
    lean = build_context_snapshot(_ctx(logs_summary=None, k8s_pod_state={}), _facts())
    for f in lean["facts"]:
        f["evidence"] = None
    lean["truncated"] = ["logs_summary", "k8s_pod_state", "facts:evidence"]
    budget = len(json.dumps({k: v for k, v in lean.items() if k != "bytes"},
                            ensure_ascii=False, default=str).encode("utf-8")) + 5
    snap = build_context_snapshot(_ctx(), _facts(), max_bytes=budget)
    assert snap["bytes"] <= budget
    assert "facts:evidence" in snap["truncated"] and "facts" not in snap["truncated"]
    assert all(f["evidence"] is None for f in snap["facts"])
    assert [f["verdict"] for f in snap["facts"]] == ["found", "absent"]


def test_weird_values_never_break_serialisation():
    class Opaque:
        def __str__(self):
            return "opaque"

    ctx = _ctx(metrics_summary={"when": datetime(2026, 9, 24), "set": {1, 2},
                                "obj": Opaque(), "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}})
    snap = build_context_snapshot(ctx, None)
    json.dumps(snap)
    assert snap["facts"] == []
    assert build_context_snapshot(None)["schema"] == SNAPSHOT_SCHEMA


def test_pipeline_capture_is_best_effort(monkeypatch):
    from app.workers import pipeline as pl

    p = pl.IncidentPipeline.__new__(pl.IncidentPipeline)
    p.incident_id = "x"
    p.fact_store = _facts()
    p.context_snapshot = {"stale": True}

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(pl, "build_context_snapshot", boom)
    p._capture_context_snapshot(_ctx())
    assert p.context_snapshot is None

    monkeypatch.undo()
    p._capture_context_snapshot(_ctx())
    assert p.context_snapshot["schema"] == SNAPSHOT_SCHEMA


# ── потребители: timeline не ломается ни со снимком, ни без ─────────────────


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


@pytest.mark.parametrize("with_snapshot", [True, False])
def test_timeline_tolerates_snapshot_presence(db, with_snapshot):
    from app.knowledge_graph.incident_timeline import build_timeline
    from app.knowledge_graph.incidents import attach_alert
    from app.knowledge_graph.populator import upsert_service
    from app.knowledge_graph.schema import AlertEvent

    t0 = datetime(2026, 9, 24, 10, 0, 0)
    svc = upsert_service(db, namespace="squad-1", name="town-service")
    db.flush()
    db.add(AlertEvent(service_id=svc.id, alertname="KubePodCrashLooping", severity="critical",
                      fingerprint="fp-s", fired_at=t0))
    db.flush()
    inc = attach_alert(db, namespace="squad-1", service_name="town-service", service_id=svc.id,
                       fired_at=t0, alertname="KubePodCrashLooping", severity="critical",
                       fingerprint="fp-s")
    analysis = {"facts": [{"kind": "process_crash", "observed": True, "confidence": 0.9,
                           "verdict": "found"}],
                "resolution_quality": "resolved", "cause": "segfault"}
    if with_snapshot:
        analysis["context_snapshot"] = build_context_snapshot(_ctx(), _facts())
    db.add(IncidentRecord(incident_id="fp-s", status="COMPLETED", data={},
                          analysis=analysis, created_at=t0 + timedelta(minutes=3)))
    db.commit()
    tl = build_timeline(db, inc, now=t0 + timedelta(minutes=30))
    assert any(e["kind"] == "evidence" for e in tl["events"])


# ── датасет: снимок накладывается на ctx правил ─────────────────────────────


def test_dataset_ctx_from_snapshot_carries_data_and_gaps():
    snap = build_context_snapshot(_ctx(logs_summary="y" * 100_000), _facts(), max_bytes=4000)
    base = {"source_status": {}, "k8s_events": None, "logs_summary": None}
    ctx = lrd.ctx_from_snapshot(dict(base), snap)
    assert ctx["k8s_pod_state"]["town-service-abc"]["exit_code"] == 139
    assert ctx["source_status"]["upstream_alerts"] == "failed: OperationalError"
    # Срезанное лимитом — пробел, а не «ничего не было».
    assert ctx["source_status"].get("logs_summary", "").startswith("truncated")


def test_dataset_ignores_unknown_schema():
    ctx = {"source_status": {}}
    assert lrd.ctx_from_snapshot(dict(ctx), {"schema": "other/v9", "k8s_events": [1]}) == ctx
    assert lrd.ctx_from_snapshot(dict(ctx), None) == ctx


# ── граф не копируется: вместо строк — ссылки ───────────────────────────────


def test_kg_rows_are_referenced_not_copied():
    ctx = _ctx(upstream_alerts=[{"alertname": "X"}],
               deploy_correlation={"deploy": {"id": 7}, "verdict": "likely", "confidence": 0.8,
                                   "metrics_diff": {"cpu_pct": {"before": 1, "after": 9}}})
    snap = build_context_snapshot(ctx, _facts())
    for copied in ("k8s_events", "recent_deployments", "upstream_alerts"):
        assert copied not in snap
    refs = snap["kg_refs"]
    assert refs["pod_events"] == {"table": "kg_pod_events", "before_min": 120, "seen": 1}
    assert refs["deployments"]["seen"] == 1
    assert refs["upstream_alerts"]["seen"] == 1
    # Вывод корреляции — да, выборка метрик под ним — нет.
    assert refs["deploy_correlation"]["verdict"] == "likely"
    assert "metrics_diff" not in refs["deploy_correlation"]


# ── датасет: реконструкция из графа ─────────────────────────────────────────


def test_reconstruct_from_kg_labels_and_redacts(monkeypatch):
    rows = [{"event_id": 1,
             "pod_events": [{"pod": "p-1", "type": "Warning", "reason": "BackOff",
                             "message": "token=abc123 for ops@example.com", "count": 5,
                             "first_seen": "2026-09-24T09:00:00", "last_seen": "2026-09-24T09:50:00"}],
             "alerts": [{"service": "s", "alertname": "KubePodCrashLooping"}],
             "deployments": [{"service": "s", "status": "SUCCESS",
                              "started_at": "2026-09-24T09:40:00", "finished_at": "2026-09-24T09:45:00"}]},
            {"event_id": 2, "pod_events": [], "alerts": [], "deployments": []}]
    monkeypatch.setattr(lrd, "_psql_rows", lambda args, sql: rows)
    cases = [{"event_id": 1}, {"event_id": 2}, {"event_id": 3, "context": "medic_observed"}]
    assert lrd.reconstruct_from_kg(object(), cases) == 1
    assert cases[0]["context"] == "kg_reconstructed"
    assert "ops@example.com" not in json.dumps(cases[0])
    assert "kg_context" not in cases[1]
    assert cases[2]["context"] == "medic_observed"   # чужую метку не перетираем


def test_reconstruct_sql_window_stops_at_medic_start():
    # Верхняя граница — начало разбора медика: события починки — это ответ.
    assert "pe.first_seen BETWEEN e.started_at - interval '7 days' AND e.started_at" in lrd._KG_SQL
    assert "+ interval" not in lrd._KG_SQL


def test_ctx_from_kg_feeds_rules_in_their_shape():
    from app.diagnostics import default_engine

    kg = {"pod_events": [{"pod": "p-1", "type": "Warning", "reason": "BackOff",
                          "message": "Back-off restarting failed container", "count": 9,
                          "last_seen": "2026-09-24T09:58:00"}],
          "deployments": [{"service": "town-service", "status": "SUCCESS",
                           "started_at": "2026-09-24T09:40:00", "finished_at": "2026-09-24T09:45:00"}],
          "alerts": [{"alertname": "KubePodCrashLooping"}]}
    ctx = _ctx(k8s_events=None, recent_deployments=None, k8s_pod_state={})
    ctx = lrd.ctx_from_kg(ctx, kg)
    assert ctx["k8s_events"][0]["reason"] == "BackOff"
    assert ctx["recent_deployments"][0]["attribution_scope"] == "namespace"
    assert "KubePodCrashLooping" in ctx["description"]
    store = default_engine.run(ctx)
    assert "recent_deploy" in store.observed_kinds()
    assert lrd.ctx_from_kg({"a": 1}, None) == {"a": 1}


# ── ревью #446: отсечка по времени снимка, агрегаты, перепрогон, метаданные ──


def test_snapshot_is_dated_and_dataset_cuts_on_that_date():
    snap = build_context_snapshot(_ctx(), _facts())
    assert datetime.fromisoformat(snap["captured_at"]).tzinfo is not None
    assert "captured_at}}')::timestamptz <= e.started_at" in lrd._SNAPSHOT_SQL
    assert "r.created_at" not in lrd._SNAPSHOT_SQL


def test_pod_event_aggregates_are_clamped_to_cutoff():
    sql = lrd._KG_SQL
    assert "least(coalesce(pe.last_seen, pe.first_seen), e.started_at) AS last_seen" in sql
    assert "THEN pe.count END AS count" in sql
    assert "(d.type = 'Warning') DESC, d.last_seen DESC" in sql


def test_reconstruction_scopes_by_squad_and_splits_statics():
    sql = lrd._KG_SQL
    # Сквад — все его ns через kg_services; вне сквадов — точный namespace.
    assert "substring(i.namespace from '^(squad-[^-]+-)') || '%'" in sql
    assert sql.count("= ANY(sc.ns)") == 4
    # Статика — не строками деплоя, а счётчиком.
    assert "NOT LIKE '%StaticsNewCluster%'" in sql and "AS statics_rollouts" in sql
    ctx = lrd.ctx_from_kg({"description": "d"}, {"pod_events": [], "deployments": [],
                                                 "alerts": [], "statics_rollouts": 7})
    assert "recent_deployments" not in ctx
    assert "статики на сквад за 6ч: 7" in ctx["description"]


def test_fact_metadata_is_redacted_and_bounded():
    leak = "password=hunter2 " + "z" * 50_000
    store = FactStore([Fact(kind="upstream_degraded", observed=False, confidence=0.0,
                            verdict="unknown", unknown_reason=leak)])
    snap = build_context_snapshot(_ctx(), store)
    reason = snap["facts"][0]["unknown_reason"]
    assert "hunter2" not in reason and len(reason) <= 300
    many = FactStore([Fact(kind=f"k{i}", observed=False, confidence=0.0, verdict="unknown",
                           unknown_reason="r" * 290) for i in range(400)])
    tight = build_context_snapshot(_ctx(), many, max_bytes=8000)
    assert tight["bytes"] <= 8000 and "facts" in tight["truncated"]


def test_run_with_ids_reruns_and_replaces_prior_result(tmp_path, monkeypatch):
    import asyncio
    import types

    out = tmp_path / "ds"
    out.mkdir()
    (out / "cases.jsonl").write_text(json.dumps({"event_id": 1}) + "\n" + json.dumps({"event_id": 2}) + "\n")
    (out / "results.jsonl").write_text(
        json.dumps({"event_id": 1, "best_cause": "old"}) + "\n"
        + json.dumps({"event_id": 2, "best_cause": "keep"}) + "\n")
    monkeypatch.setattr(lrd, "REPO_ROOT", Path("/nonexistent-repo-root"))

    async def fake_run(case, mode="alert_only"):
        return {"event_id": case["event_id"], "context": mode, "best_cause": "new"}

    monkeypatch.setattr(lrd, "_run_case", fake_run)
    args = types.SimpleNamespace(out=str(out), ids="1", limit=5)
    asyncio.run(lrd._run_async(args))
    rows = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines() if ln]
    by = {r["event_id"]: r["best_cause"] for r in rows}
    assert by == {1: "new", 2: "keep"} and len(rows) == 2
