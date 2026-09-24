"""Контракт сборщиков контекста: CollectorResult → source_status.

Раннер (исключение, таймаут, None, пустота), вывод записей Known Unknowns и
перевод сборщиков пайплайна: упавший k8s-снапшот больше не даёт правилам
уверенного ✗.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.context.collector import (Collector, CollectorResult, Outcome,
                                   SourceStatus, merge_source_status)
from app.context.k8s_facts import K8sSnapshot
from app.diagnostics.facts import Verdict
from app.diagnostics.rules.oom import OOMKilledRule

_KG = Collector(
    name="upstream_alerts",
    ctx_fields=("upstream_alerts",),
    provenance="kg_alerts",
    failure_label="kg_alerts недоступен",
)


class OperationalError(Exception):
    """Имя как у sqlalchemy: причина в source_status называет тип исключения."""


def _boom(*_a: Any, **_kw: Any) -> Any:
    raise OperationalError("connection refused")


# --- синхронный раннер ---------------------------------------------------


def test_exception_is_failed_with_label_and_exception_type():
    res = _KG.run_sync(_boom)
    assert res.status is SourceStatus.FAILED
    assert res.error == "OperationalError"
    assert res.data is None
    # Формат прежнего кода дословно — его читают embed и timeline.
    assert res.source_status_entries() == {
        "upstream_alerts": "kg_alerts недоступен: OperationalError",
    }


def test_failure_reason_overrides_label():
    c = Collector(
        name="node_namespaces", ctx_fields=("node_namespaces",),
        failure_reason="k8s API не ответил",
    )
    assert c.run_sync(_boom).source_status_entries() == {
        "node_namespaces": "k8s API не ответил",
    }


def test_none_is_unavailable_with_declared_reason():
    c = Collector(
        name="node_namespaces", ctx_fields=("node_namespaces",),
        unavailable_reason="k8s API не ответил",
    )
    res = c.run_sync(lambda: None)
    assert res.status is SourceStatus.UNAVAILABLE
    assert res.source_status_entries() == {"node_namespaces": "k8s API не ответил"}


def test_none_without_reason_falls_back_to_status_value():
    res = Collector(name="x", ctx_fields=("x",)).run_sync(lambda: None)
    assert res.source_status_entries() == {"x": "unavailable"}


def test_empty_is_observation_without_entry():
    # «Опрошено, пусто» — законный ABSENT, записи в source_status быть не должно.
    res = _KG.run_sync(lambda: [])
    assert res.status is SourceStatus.EMPTY
    assert res.data == []
    assert res.ok
    assert res.source_status_entries() == {}


def test_data_is_success():
    res = _KG.run_sync(lambda a, b=0: [a, b], 1, b=2)
    assert res.status is SourceStatus.SUCCESS
    assert res.data == [1, 2]
    assert res.source_status_entries() == {}


def test_partial_keeps_data_without_entry():
    res = _KG.run_sync(lambda: Outcome(SourceStatus.PARTIAL, [1]))
    assert res.status is SourceStatus.PARTIAL
    assert res.data == [1]
    assert res.source_status_entries() == {}


def test_explicit_outcome_carries_data_and_reason():
    # Мёртвый поток деплоев: данные ([]) есть, но пустота — пробел.
    res = _KG.run_sync(
        lambda: Outcome(SourceStatus.UNAVAILABLE, [], reason="поток стоит"),
    )
    assert res.data == []
    assert res.source_status_entries() == {"upstream_alerts": "поток стоит"}


def test_to_dict_has_no_data_and_carries_provenance():
    d = _KG.run_sync(lambda: [1]).to_dict()
    assert "data" not in d
    assert d["status"] == "success"
    assert d["provenance"] == "kg_alerts"
    assert d["duration_ms"] >= 0


# --- асинхронный раннер --------------------------------------------------


def test_async_timeout_is_unavailable_with_reason():
    c = Collector(
        name="vm", ctx_fields=("metrics_summary",),
        timeout_seconds=0.01, failure_label="VictoriaMetrics недоступна",
    )

    async def slow() -> Dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    res = asyncio.run(c.run(slow))
    # Таймаут — не FAILED: исключения по дороге не было, ответа нет.
    assert res.status is SourceStatus.UNAVAILABLE
    assert res.error == "TimeoutError"
    assert res.source_status_entries() == {
        "metrics_summary": "VictoriaMetrics недоступна: таймаут 0.01с",
    }


def test_async_exception_is_failed():
    async def broken() -> Any:
        raise OperationalError()

    res = asyncio.run(_KG.run(broken))
    assert res.status is SourceStatus.FAILED
    assert res.reason == "kg_alerts недоступен: OperationalError"


def test_async_success_with_classify():
    async def ok() -> str:
        return "snap"

    res = asyncio.run(_KG.run(
        ok, classify=lambda d: Outcome(SourceStatus.PARTIAL, d),
    ))
    assert res.status is SourceStatus.PARTIAL
    assert res.data == "snap"


# --- merge ---------------------------------------------------------------


def test_merge_without_overwrite_keeps_specific_reason():
    status = {"upstream_alerts": "kg_alerts недоступен: OperationalError"}
    not_in_kg = CollectorResult(
        name="kg_service_lookup", status=SourceStatus.UNAVAILABLE,
        ctx_fields=("upstream_alerts", "k8s_events"),
        reason="сервис не найден в KG — источник не опрошен",
    )
    merge_source_status(status, not_in_kg, overwrite=False)
    assert status == {
        "upstream_alerts": "kg_alerts недоступен: OperationalError",
        "k8s_events": "сервис не найден в KG — источник не опрошен",
    }


def test_merge_of_ok_result_writes_nothing():
    status: Dict[str, str] = {}
    merge_source_status(status, _KG.run_sync(lambda: []))
    assert status == {}


# --- пайплайн: k8s-снапшот и VM ------------------------------------------


def _pipeline_stub() -> Any:
    from app.workers.pipeline import IncidentPipeline
    p = IncidentPipeline.__new__(IncidentPipeline)
    p.incident = SimpleNamespace(
        namespace="squad-1", labels={"pod": "api-7f9"}, starts_at=None,
    )
    p.incident_id = "inc-1"
    p.collector_results = []
    return p


def _pipeline_ctx() -> Dict[str, Any]:
    return {"pod": "api-7f9", "k8s_summary": None, "source_status": {}}


def test_failed_k8s_snapshot_marks_fields_and_turns_absent_into_unknown(monkeypatch):
    from app.workers import pipeline as pl

    async def failed_snapshot(namespace: str, pod: Any = None) -> K8sSnapshot:
        return K8sSnapshot(
            text="[k8s_facts unavailable: Forbidden]", error="ApiException",
        )

    monkeypatch.setattr(pl.K8sFacts, "collect_snapshot", failed_snapshot)
    p = _pipeline_stub()
    ctx = _pipeline_ctx()
    asyncio.run(p._enrich_k8s(ctx))

    # Заглушка по-прежнему в logs_summary (её видит LLM), но поля помечены.
    assert ctx["logs_summary"].startswith("[k8s_facts unavailable")
    reason = "k8s API недоступен: ApiException"
    assert ctx["source_status"] == {
        "logs_summary": reason, "k8s_pod_state": reason, "k8s_events": reason,
    }
    assert p.collector_results[0].status is SourceStatus.FAILED

    # Раньше: ✗ «OOM не было» по заглушке. Теперь: ? — API не видели.
    facts = OOMKilledRule().run(ctx)
    assert facts and all(f.verdict == Verdict.UNKNOWN for f in facts)

    # Контроль: без записи тот же ctx давал уверенный ABSENT.
    ctx_old = dict(ctx, source_status={})
    old = OOMKilledRule().run(ctx_old)
    assert any(f.verdict == Verdict.ABSENT for f in old)


def test_healthy_k8s_snapshot_writes_no_entry(monkeypatch):
    from app.workers import pipeline as pl

    async def ok_snapshot(namespace: str, pod: Any = None) -> K8sSnapshot:
        return K8sSnapshot(text="Unhealthy pods in squad-1: none")

    monkeypatch.setattr(pl.K8sFacts, "collect_snapshot", ok_snapshot)
    p = _pipeline_stub()
    ctx = _pipeline_ctx()
    asyncio.run(p._enrich_k8s(ctx))
    assert ctx["source_status"] == {}
    assert ctx["k8s_events"] == []
    assert p.collector_results[0].status is SourceStatus.SUCCESS


def test_k8s_snapshot_exception_is_audited_and_marked(monkeypatch):
    from app.workers import pipeline as pl

    async def raising(namespace: str, pod: Any = None) -> K8sSnapshot:
        raise OperationalError()

    events = []
    monkeypatch.setattr(pl.K8sFacts, "collect_snapshot", raising)
    monkeypatch.setattr(
        pl.audit_service, "log_event", lambda name, data: events.append((name, data)),
    )
    p = _pipeline_stub()
    ctx = _pipeline_ctx()
    asyncio.run(p._enrich_k8s(ctx))
    assert "logs_summary" not in ctx
    assert ctx["source_status"]["k8s_events"] == "k8s API недоступен: OperationalError"
    assert events == [(
        "K8S_ENRICHMENT_FAILED", {"incident_id": "inc-1", "error": "OperationalError"},
    )]


def test_vm_not_configured_marks_metrics_as_unknown(monkeypatch):
    from app.workers import pipeline as pl

    monkeypatch.setattr(pl.settings, "VICTORIA_METRICS_URL", "", raising=False)
    p = _pipeline_stub()
    ctx = _pipeline_ctx()
    asyncio.run(p._enrich_vm(ctx))
    assert ctx["source_status"] == {"metrics_summary": "VictoriaMetrics не настроен"}
    assert "metrics_summary" not in ctx


@pytest.mark.parametrize("raises", [True, False])
def test_diagnostics_ctx_upstream_alerts_status(monkeypatch, raises):
    from datetime import datetime, timezone

    from app.diagnostics import incident_ctx
    from app.models.incident import Incident

    def fake_nearby(*_a: Any, **_kw: Any) -> Any:
        if raises:
            raise OperationalError()
        return []

    monkeypatch.setattr(incident_ctx, "nearby_alerts", fake_nearby)
    inc = Incident(
        incident_id="fp-collector", severity="warning", status="firing",
        summary="test", namespace="squad-1",
        labels={"service": "api", "alertname": "KubePodCrashLooping"},
        annotations={}, starts_at=datetime.now(timezone.utc).isoformat(),
    )
    ctx = incident_ctx.build_diagnostics_ctx(inc, "", kg_session=object())
    if raises:
        assert ctx["upstream_alerts"] is None
        assert ctx["source_status"] == {
            "upstream_alerts": "kg_alerts недоступен: OperationalError",
        }
    else:
        # Граф опрошен, соседи молчат — ABSENT, записи нет.
        assert ctx["upstream_alerts"] == []
        assert ctx["source_status"] == {}
