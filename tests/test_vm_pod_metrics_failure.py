"""Сбой VictoriaMetrics — не «давления нет».

`VMClient.get_pod_metrics` раньше глушил любой отказ в нулевой result
(`memory_pressure: False`), и ResourcePressureRule уверенно отвечал ✗ по
метрикам, которых не видел. Теперь отказ доходит до CollectorResult и
source_status, а правило отвечает ?. Здоровый путь не меняется.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from app.context.collector import SourceStatus
from app.context.vm_client import VMClient, VMQueryError
from app.diagnostics.facts import Verdict
from app.diagnostics.rules.resource_pressure import ResourcePressureRule


def _series(value: float) -> List[Dict[str, Any]]:
    return [{"metric": {}, "values": [[0, str(value)]] * 6}]


def _fake_range(responses: Dict[str, Any]):
    """query_range, отвечающий по подстроке запроса: значение или исключение."""
    async def fake(self: VMClient, query: str, start: Any, end: Any, step: str = "60s"):
        for marker, resp in responses.items():
            if marker in query:
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        raise AssertionError(f"unexpected query {query}")
    return fake


_DOWN = httpx.ConnectError("connection refused")
# Память на 95 % лимита, CPU почти не троттлится.
_MEM_HIGH = {
    "container_memory_working_set_bytes": _series(950),
    "kube_pod_container_resource_limits": _series(1000),
}
_CPU_LOW = {"container_cpu_cfs_throttled": _series(0.01)}
_CPU_HIGH = {"container_cpu_cfs_throttled": _series(0.9)}
_MEM_LOW = {
    "container_memory_working_set_bytes": _series(100),
    "kube_pod_container_resource_limits": _series(1000),
}


def _pod_metrics(monkeypatch, responses: Dict[str, Any], pod: str = "api-7f9"):
    monkeypatch.setattr(VMClient, "query_range", _fake_range(responses))
    return asyncio.run(VMClient("http://vm").get_pod_metrics("squad-1", pod))


# --- клиент ----------------------------------------------------------------


def test_all_queries_failed_raises_instead_of_zeros(monkeypatch):
    responses = {
        "container_memory_working_set_bytes": _DOWN,
        "kube_pod_container_resource_limits": _DOWN,
        "container_cpu_cfs_throttled": _DOWN,
    }
    with pytest.raises(VMQueryError):
        _pod_metrics(monkeypatch, responses)


def test_partial_failure_marks_signal_unknown_and_keeps_the_other(monkeypatch):
    res = _pod_metrics(monkeypatch, {**_MEM_HIGH, "container_cpu_cfs_throttled": _DOWN})
    assert res["memory_pressure"] is True
    assert res["cpu_pressure"] is None
    assert res["vm_errors"] == {"cpu": "ConnectError"}


def test_failed_limit_query_makes_memory_unknown(monkeypatch):
    res = _pod_metrics(monkeypatch, {
        "container_memory_working_set_bytes": _series(950),
        "kube_pod_container_resource_limits": _DOWN,
        **_CPU_LOW,
    })
    assert res["memory_pressure"] is None
    assert res["cpu_pressure"] is False
    assert res["vm_errors"] == {"memory": "ConnectError"}


def test_garbage_response_raises(monkeypatch):
    bad = [{"metric": {}, "values": [[0, "not-a-number"]]}]
    with pytest.raises(VMQueryError):
        _pod_metrics(monkeypatch, {**_MEM_LOW, "container_cpu_cfs_throttled": bad})


def test_invalid_label_raises_instead_of_zeros(monkeypatch):
    with pytest.raises(ValueError):
        _pod_metrics(monkeypatch, {}, pod='x"}or vector(1)')


def test_success_is_unchanged(monkeypatch):
    res = _pod_metrics(monkeypatch, {**_MEM_LOW, **_CPU_LOW})
    assert res["memory_pressure"] is False
    assert res["cpu_pressure"] is False
    assert "vm_errors" not in res


# --- пайплайн --------------------------------------------------------------


def _run_enrich_vm(monkeypatch, responses: Dict[str, Any], *,
                   labels: Dict[str, str] | None = None,
                   instant: Any = 1.0) -> tuple[Any, Dict[str, Any]]:
    from app.workers import pipeline as pl

    async def fake_instant(self: VMClient, query: str):
        return instant

    monkeypatch.setattr(pl.settings, "VICTORIA_METRICS_URL", "http://vm", raising=False)
    monkeypatch.setattr(VMClient, "query_range", _fake_range(responses))
    monkeypatch.setattr(VMClient, "query_instant", fake_instant)
    monkeypatch.setattr(pl.audit_service, "log_event", lambda *a, **kw: None)

    p = pl.IncidentPipeline.__new__(pl.IncidentPipeline)
    p.incident = SimpleNamespace(
        namespace="squad-1",
        labels=labels if labels is not None else {"pod": "api-7f9"},
        starts_at=None,
    )
    p.incident_id = "inc-1"
    p.collector_results = []
    p.cluster_health_context = ""
    ctx: Dict[str, Any] = {"namespace": "squad-1", "source_status": {}}
    asyncio.run(p._enrich_vm(ctx))
    return p, ctx


def _verdicts(ctx: Dict[str, Any]) -> set:
    return {f.verdict for f in ResourcePressureRule().run(ctx)}


def test_vm_down_turns_absent_into_unknown(monkeypatch):
    responses = {
        "container_memory_working_set_bytes": _DOWN,
        "kube_pod_container_resource_limits": _DOWN,
        "container_cpu_cfs_throttled": _DOWN,
    }
    p, ctx = _run_enrich_vm(monkeypatch, responses, instant=None)

    assert ctx["source_status"]["metrics_summary"] == "VictoriaMetrics недоступна: VMQueryError"
    assert ctx["source_status"]["cluster_health"].startswith("VictoriaMetrics недоступна")
    # Пустой снимок кластера по-прежнему доходит до LLM как UNKNOWN.
    assert "UNKNOWN" in ctx["cluster_health_context"]
    assert _verdicts(ctx) == {Verdict.UNKNOWN}


def test_partial_failure_keeps_found_pressure(monkeypatch):
    _, ctx = _run_enrich_vm(monkeypatch, {**_MEM_HIGH, "container_cpu_cfs_throttled": _DOWN})
    assert ctx["source_status"]["metrics_summary"] == (
        "VictoriaMetrics недоступна частично (cpu: ConnectError)"
    )
    # Найденное давление памяти остаётся найденным.
    assert _verdicts(ctx) == {Verdict.FOUND}


def test_partial_failure_without_pressure_is_unknown(monkeypatch):
    _, ctx = _run_enrich_vm(monkeypatch, {
        "container_memory_working_set_bytes": _DOWN,
        "kube_pod_container_resource_limits": _series(1000),
        **_CPU_LOW,
    })
    assert "metrics_summary" in ctx["source_status"]
    assert _verdicts(ctx) == {Verdict.UNKNOWN}


def test_missing_pod_label_is_not_zeros(monkeypatch):
    p, ctx = _run_enrich_vm(monkeypatch, {}, labels={})
    assert ctx["source_status"]["metrics_summary"] == (
        "метрики пода не опрошены: нет валидной метки pod"
    )
    assert "metrics_summary" not in ctx
    assert _verdicts(ctx) == {Verdict.UNKNOWN}


def test_healthy_vm_writes_no_entry_and_keeps_absent(monkeypatch):
    p, ctx = _run_enrich_vm(monkeypatch, {**_MEM_LOW, **_CPU_LOW})
    assert ctx["source_status"] == {}
    assert ctx["metrics_summary"]["memory_pressure"] is False
    assert {r.status for r in p.collector_results} == {SourceStatus.SUCCESS}
    assert _verdicts(ctx) == {Verdict.ABSENT}
