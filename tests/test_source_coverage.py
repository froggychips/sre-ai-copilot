"""Покрытие источников в analysis: компактные сводки сборщиков.

`collector_results` жили только в памяти пайплайна и пропадали после
прогона; теперь их сводка (без data) уходит в analysis.source_coverage и
переживает resume из checkpoint-а.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.context.collector import CollectorResult, SourceStatus, summarize_coverage
from app.models.incident import Incident


def _result(name: str, status: SourceStatus, fields: list, reason: Any = None) -> CollectorResult:
    return CollectorResult(
        name=name, status=status, ctx_fields=tuple(fields), provenance=f"src:{name}",
        data=["большие данные"], reason=reason,
    )


def _pipeline_stub() -> Any:
    from app.workers.pipeline import IncidentPipeline
    p = IncidentPipeline.__new__(IncidentPipeline)
    p.incident = SimpleNamespace(
        namespace="squad-1", labels={"pod": "api-7f9"}, starts_at=None,
        teamcity_context="tc",
    )
    p.incident_id = "inc-1"
    p.collector_results = []
    p._restored_collectors = []
    p.traces = []
    return p


def test_compact_dict_drops_data_and_timestamps():
    d = _result("k8s_snapshot", SourceStatus.SUCCESS, ["k8s_events"]).to_compact_dict()
    assert "data" not in d and "started_at" not in d and "finished_at" not in d
    assert d["name"] == "k8s_snapshot" and d["status"] == "success"
    assert d["ctx_fields"] == ["k8s_events"] and isinstance(d["duration_ms"], int)


def test_coverage_problem_is_sticky_and_empty_is_covered():
    cov = summarize_coverage([
        _result("k8s_snapshot", SourceStatus.FAILED, ["k8s_events"],
                reason="k8s API недоступен: ApiException").to_compact_dict(),
        # Соседний успех по тому же полю не отменяет пробела: правило ответило ?.
        _result("pod_events_kg", SourceStatus.SUCCESS, ["k8s_events"]).to_compact_dict(),
        _result("upstream_alerts", SourceStatus.EMPTY, ["upstream_alerts"]).to_compact_dict(),
    ])
    assert cov["problems"] == 1 and len(cov["collectors"]) == 3
    assert cov["fields"]["k8s_events"] == {
        "status": "failed", "collector": "k8s_snapshot",
        "reason": "k8s API недоступен: ApiException",
    }
    # «Опрошено, пусто» — покрыто, без причины (в source_status его нет).
    assert cov["fields"]["upstream_alerts"] == {
        "status": "empty", "collector": "upstream_alerts", "reason": None,
    }


def test_diagnostics_ctx_hands_upstream_run_to_pipeline(monkeypatch):
    from app.diagnostics import incident_ctx

    monkeypatch.setattr(incident_ctx, "nearby_alerts", lambda *a, **k: [])
    inc = Incident(
        incident_id="fp-cov", severity="warning", status="firing",
        summary="test", namespace="squad-1",
        labels={"service": "api", "alertname": "KubePodCrashLooping"},
        annotations={}, starts_at=datetime.now(timezone.utc).isoformat(),
    )
    ctx = incident_ctx.build_diagnostics_ctx(inc, "", kg_session=object())
    runs = ctx[incident_ctx.COLLECTOR_RESULTS_KEY]
    assert [r.name for r in runs] == ["upstream_alerts"]
    assert runs[0].status is SourceStatus.EMPTY


def test_pipeline_coverage_survives_checkpoint_resume():
    """Resume после ретрая не перезапускает diagnose — покрытие из checkpoint-а."""
    from app.workers.pipeline import _CHECKPOINT_KEY

    p = _pipeline_stub()
    p.collector_results = [
        _result("vm_pod_metrics", SourceStatus.UNAVAILABLE, ["metrics_summary"],
                reason="VictoriaMetrics не настроена"),
    ]
    cp = {"summary": "s", "facts": [], "collectors": p._collector_summaries()}

    resumed = _pipeline_stub()
    resumed.record = SimpleNamespace(analysis={_CHECKPOINT_KEY: cp})
    assert resumed._restore_checkpoint(frozenset({"analyze", "diagnose"})) is True
    cov = resumed._source_coverage()
    assert [c["name"] for c in cov["collectors"]] == ["vm_pod_metrics"]
    assert cov["fields"]["metrics_summary"]["reason"] == "VictoriaMetrics не настроена"


def test_failed_restore_does_not_leak_stale_collectors():
    """diagnose восстановился, critique — нет: откат в полный перезапуск.

    Старые сводки не должны пережить откат — иначе новый прогон diagnose
    задвоит опросы, а «липкий» пробел из старого сбоя останется висеть.
    """
    from app.workers.pipeline import _CHECKPOINT_KEY

    stale = _result("vm_pod_metrics", SourceStatus.FAILED, ["metrics_summary"],
                    reason="старый сбой").to_compact_dict()
    resumed = _pipeline_stub()
    resumed.record = SimpleNamespace(analysis={_CHECKPOINT_KEY: {
        "summary": "s", "facts": [], "collectors": [stale],
        # critique в completed, но данных под него нет → restore == False.
        "critiqued": None,
    }})
    assert resumed._restore_checkpoint(
        frozenset({"analyze", "diagnose", "critique"}),
    ) is False
    assert resumed._restored_collectors == []


def test_restore_without_collectors_key_is_empty_coverage():
    """Checkpoint прежних версий ключа не несёт — resume не падает."""
    from app.workers.pipeline import _CHECKPOINT_KEY

    resumed = _pipeline_stub()
    resumed.record = SimpleNamespace(
        analysis={_CHECKPOINT_KEY: {"summary": "s", "facts": []}},
    )
    assert resumed._restore_checkpoint(frozenset({"analyze", "diagnose"})) is True
    assert resumed._source_coverage() == {"collectors": [], "fields": {}, "problems": 0}
