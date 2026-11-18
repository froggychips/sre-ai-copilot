"""Мост: вид цели до kubectl, `match.target_kinds`, ранний блок policy.

Прод 18.11.2026: `scale_out_cpu_throttled_deployment` матчился на поды
StatefulSet-ов баз (`<имя>-postgresql-0`), мост на каждый алерт снимал
K8sFacts и делал `describe deployment`, получал «live replicas unknown» и
писал info-строку — тысячи в сутки, ноль предложений.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.diagnostics.facts import Fact, FactStore
from app.models.incident import Incident
from app.remediation import bridge
from app.remediation.matcher import (ANY_TARGET_KIND, default_registry,
                                     match_playbooks, target_kind_rejection)
from app.remediation.playbook import Playbook
from app.services.alert_enrichment import EnrichedContext

_SCALE = "scale_out_cpu_throttled_deployment"
_RESTART = "restart_crashloop_deployment"
_DEPLOY_POD = "town-service-6c6cd4df-8hx9c"
_STS_POD = "town-db-postgresql-0"


@pytest.fixture
def db():
    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401
    from app.remediation import models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def probe(monkeypatch):
    """Счётчик проб kubectl (живые реплики цели)."""
    calls: list = []

    def _probe(intent):
        calls.append((intent.resource_type, intent.resource_name))
        return 2

    monkeypatch.setattr(bridge, "_probe_current_replicas", _probe)
    return calls


def _skipped(reason: str, playbook: str, kind: str) -> float:
    from app.metrics import REMEDIATION_BRIDGE_SKIPPED
    return REMEDIATION_BRIDGE_SKIPPED.labels(
        reason=reason, playbook=playbook, target_kind=kind)._value.get()


def _incident(pod: str, namespace: str, alertname: str = "CPUThrottlingHigh",
              **labels) -> Incident:
    return Incident(
        incident_id="fp-kind-1", severity="info", status="firing",
        summary=alertname, description="", namespace=namespace,
        labels={"alertname": alertname, "namespace": namespace, "pod": pod,
                "container": "main", **labels},
        annotations={}, starts_at="2026-11-18T10:00:00Z",
    )


def _ctx(pod: str = _DEPLOY_POD, namespace: str = "squad-7-k5",
         service: str = "town-service", **labels) -> EnrichedContext:
    return EnrichedContext(
        incident=_incident(pod, namespace, **labels), service=service, pod_name=pod,
        rule_facts=[Fact(kind="recent_deploy", observed=False, confidence=0.9)],
    )


def _facts(ctx: EnrichedContext) -> FactStore:
    snap = SimpleNamespace(text="Unhealthy pods: none", container_terminated={},
                           pod_events=[], error=None, unhealthy_pods=[])
    store = FactStore(list(ctx.rule_facts))
    store.extend(bridge.pod_state_facts(ctx, snap))
    return store


def _workload(db, name: str, kind: str, namespace: str = "squad-7-k5") -> None:
    from app.knowledge_graph.schema import NODE_KIND_WORKLOAD, Service
    db.add(Service(name=name, namespace=namespace, node_kind=NODE_KIND_WORKLOAD,
                   metadata_json={"k8s_workload": {"workload_kind": kind}}))
    db.commit()


# ── схема и matcher ───────────────────────────────────────────────────────

def test_registry_playbooks_declare_deployment_only():
    reg = default_registry()
    assert reg[_SCALE].match.target_kinds == ["Deployment"]
    assert reg[_RESTART].match.target_kinds == ["Deployment"]


def _pb(**match) -> dict:
    return {
        "schema_version": "remediation.playbook/v2", "name": "x", "kind": "remediation",
        "match": {"alertnames": ["A"], **match},
        "policy": {"approve": {"namespace_tier": ["squad"]}},
        "plan": {"steps": [{"action": "restart_deployment"}]},
    }


def test_target_kinds_is_optional_and_validated():
    assert Playbook.model_validate(_pb()).match.target_kinds is None
    assert Playbook.model_validate(
        _pb(target_kinds=["StatefulSet", "Cluster"])).match.target_kinds == ["StatefulSet", "Cluster"]
    for bad in (["deployment"], ["Pod"], []):
        with pytest.raises(ValueError):
            Playbook.model_validate(_pb(target_kinds=bad))


def test_target_kinds_is_v2_only():
    v1 = {
        "schema_version": "remediation.playbook/v1", "name": "x", "kind": "remediation",
        "match": {"classification": "stale_failed_job", "target_kinds": ["Job"]},
        "policy": {}, "plan": {"command": ["kubectl", "get", "job"]},
    }
    with pytest.raises(ValueError, match="match.target_kinds"):
        Playbook.model_validate(v1)


def test_rejection_reasons():
    declared = Playbook.model_validate(_pb(target_kinds=["Deployment"]))
    undeclared = Playbook.model_validate(_pb())
    assert target_kind_rejection(declared, "Deployment") is None
    assert target_kind_rejection(declared, "StatefulSet") == "target_kind_mismatch"
    assert target_kind_rejection(declared, None) == "target_kind_unknown"
    assert target_kind_rejection(declared, ANY_TARGET_KIND) is None
    # Поле не задано — вид не проверяется вовсе (обратная совместимость).
    assert target_kind_rejection(undeclared, "StatefulSet") is None
    assert target_kind_rejection(undeclared, None) is None


def test_match_playbooks_filters_by_kind_before_preconditions():
    ctx = _ctx()
    facts = _facts(ctx)
    names = lambda **kw: [pb.name for pb in match_playbooks(  # noqa: E731
        alertname="CPUThrottlingHigh", facts=facts, **kw)]
    assert names() == [_SCALE]                           # по умолчанию — как раньше
    assert names(target_kind="Deployment") == [_SCALE]
    rejected: list = []
    assert names(target_kind="StatefulSet", rejected=rejected) == []
    assert rejected == [(_SCALE, "target_kind_mismatch")]
    rejected = []
    # Отсев по виду не ждёт фактов: preconditions не выполнены, а причина — вид.
    assert match_playbooks(alertname="CPUThrottlingHigh", facts=FactStore([]),
                           target_kind=None, rejected=rejected) == []
    assert rejected == [(_SCALE, "target_kind_unknown")]


# ── plan_proposals ────────────────────────────────────────────────────────

def test_statefulset_target_is_skipped_without_kubectl(db, probe, capsys):
    """Главный случай прода: под базы в скваде."""
    ctx = _ctx(pod=_STS_POD, service="town-db-postgresql")
    before = _skipped("target_kind_mismatch", _SCALE, "StatefulSet")
    assert bridge.plan_proposals(ctx, _facts(ctx), db) == []
    assert probe == []
    assert _skipped("target_kind_mismatch", _SCALE, "StatefulSet") == before + 1
    assert "plan_render_failed" not in capsys.readouterr().out


def test_graph_kind_overrides_pod_name_shape(db, probe):
    """Под выглядит как под Deployment-а, но граф знает, что это DaemonSet."""
    _workload(db, "node-agent-bcdfghjk", "DaemonSet")
    ctx = _ctx(pod="node-agent-bcdfghjk-x7k2p", service="node-agent")
    assert bridge.plan_proposals(ctx, _facts(ctx), db) == []
    assert probe == []


def test_unknown_kind_is_skipped_fail_closed(db, probe):
    ctx = _ctx(pod="standalone", service="standalone")
    before = _skipped("target_kind_unknown", _SCALE, "unknown")
    assert bridge.plan_proposals(ctx, _facts(ctx), db) == []
    assert probe == []
    assert _skipped("target_kind_unknown", _SCALE, "unknown") == before + 1


def test_deployment_name_comes_from_pod_not_from_service(db, probe):
    """Сервис обогащения назван иначе, чем Deployment пода."""
    _workload(db, "town-service", "Deployment")
    ctx = _ctx(service="town")            # имя k8s Service ≠ имя Deployment-а
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.resource_name == "town-service" and p.mode == "approve"
    assert p.target_ref["kind"] == "Deployment" and p.target_ref["name"] == "town-service"
    assert probe and {name for _, name in probe} == {"town-service"}
    assert "town-service" in p.command_preview


def test_deployment_label_still_wins(db, probe):
    ctx = _ctx(service="other", deployment="town-api")
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.resource_name == "town-api"


def test_flag_off_restores_legacy_name_and_probe(db, probe, monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_TARGET_KIND_ENABLED", False)
    ctx = _ctx(pod=_STS_POD, service="town-db-postgresql")
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    # Как в 1.0.35: имя сервиса принимается за Deployment, блок по data-plane.
    assert p.resource_name == "town-db-postgresql" and p.mode == "block"


def test_plan_render_failure_is_counted(db, monkeypatch):
    monkeypatch.setattr(bridge, "_probe_current_replicas", lambda intent: None)
    ctx = _ctx()
    before = _skipped("plan_render_failed", _SCALE, "Deployment")
    assert bridge.plan_proposals(ctx, _facts(ctx), db) == []
    assert _skipped("plan_render_failed", _SCALE, "Deployment") == before + 1


def test_invalid_intent_does_not_abort_planning(db, probe):
    """Запретный namespace: валидатор ExecutionIntent отказывает — предложения
    нет, но исключение наружу не летит (было `plan_failed error=ValidationError`)."""
    from app.core.execution_dsl import FORBIDDEN_NAMESPACES
    ns = sorted(FORBIDDEN_NAMESPACES)[0]
    ctx = _ctx(namespace=ns)
    before = _skipped("plan_render_failed", _SCALE, "Deployment")
    assert bridge.plan_proposals(ctx, _facts(ctx), db) == []
    assert _skipped("plan_render_failed", _SCALE, "Deployment") == before + 1


# ── ранний блок policy ────────────────────────────────────────────────────

@pytest.mark.parametrize("namespace,tier,pod,service,axis", [
    ("prod-kingdom5", "prod", _DEPLOY_POD, "town-service", "namespace_tier"),
    ("sre-ai", "system", "copilot-worker-8596b5496-fnrxt", "copilot-worker", "namespace_tier"),
    # Сквад, но Deployment бэкапа базы: data-plane по имени.
    ("squad-7-k5", "squad", "message-db-backup-cc4696c69-dzzqd", "message-db-backup", "data_plane"),
])
def test_static_policy_block_skips_the_probe(db, probe, namespace, tier, pod, service, axis):
    ctx = _ctx(pod=pod, namespace=namespace, service=service)
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.mode == "block" and p.blocked and p.early_block and p.tier == tier
    assert probe == []
    assert axis in {r["axis"] for r in p.reasons}
    assert all(r["rule"] == "block.any" for r in p.reasons)
    assert "--replicas" not in p.command_preview
    assert p.blocked_line() == f"playbook `{_SCALE}`: заблокирован policy ({tier})"


def test_early_block_reasons_equal_full_gate_reasons(db, probe, monkeypatch):
    """Ранний блок не меняет решение: те же причины, что дал бы полный путь."""
    ctx = _ctx(namespace="prod-kingdom5")
    [early] = bridge.plan_proposals(ctx, _facts(ctx), db)
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_EARLY_POLICY_BLOCK_ENABLED", False)
    [full] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert probe, "без раннего блока проба идёт"
    assert not full.early_block and full.mode == early.mode == "block"
    assert full.reasons == early.reasons
    assert "--replicas=3" in full.command_preview


def test_approve_tier_still_probes_and_is_not_early(db, probe):
    ctx = _ctx()
    [p] = bridge.plan_proposals(ctx, _facts(ctx), db)
    assert p.mode == "approve" and not p.early_block
    assert p.intent.params == {"replicas": 3, "current_replicas": 2}
    assert probe


@pytest.mark.asyncio
async def test_early_block_is_recorded_as_decision(db, probe, monkeypatch):
    from app.remediation.models import RemediationDecision
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", True)
    ctx = _ctx(namespace="prod-kingdom5")
    snap = SimpleNamespace(text="", container_terminated={}, pod_events=[],
                           error=None, unhealthy_pods=[])
    monkeypatch.setattr("app.context.k8s_facts.K8sFacts.collect_snapshot",
                        AsyncMock(return_value=snap))
    out = await bridge.run_bridge(db, ctx)
    assert out == [{"playbook": _SCALE, "mode": "block", "outcome": "blocked"}]
    [row] = db.query(RemediationDecision).all()
    assert row.decision == "block" and row.selected_playbook == _SCALE
    assert row.decision_reasons[0]["axis"] == "namespace_tier"
    assert probe == []


# ── отсев до обогащения ───────────────────────────────────────────────────

def test_incompatible_kind_precheck(db):
    sts = _incident(_STS_POD, "squad-7-k5").labels
    dep = _incident(_DEPLOY_POD, "squad-7-k5").labels
    odd = _incident("standalone", "squad-7-k5").labels
    assert bridge.target_kind_incompatible(sts, "squad-7-k5", db) is True
    assert bridge.target_kind_incompatible(dep, "squad-7-k5", db) is False
    # Вид не определён — решит plan_proposals после обогащения.
    assert bridge.target_kind_incompatible(odd, "squad-7-k5", db) is False
    assert bridge.target_kind_incompatible({"alertname": "Nope", "pod": _STS_POD}, "x", db) is False


def test_precheck_respects_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_TARGET_KIND_ENABLED", False)
    labels = _incident(_STS_POD, "squad-7-k5").labels
    assert bridge.target_kind_incompatible(labels, "squad-7-k5", db) is False


@pytest.mark.asyncio
async def test_task_skips_enrichment_and_k8s_for_statefulset(db, monkeypatch):
    """Задача воркера: ни обогащения, ни K8sFacts, ни run_bridge."""
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_ENABLED", True)
    enrich = AsyncMock()
    run = AsyncMock(return_value=[])
    k8s = AsyncMock()
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    monkeypatch.setattr("app.services.alert_enrichment.enrich_alert_async", enrich)
    monkeypatch.setattr("app.context.k8s_facts.K8sFacts.collect_snapshot", k8s)
    monkeypatch.setattr(bridge, "run_bridge", run)
    before = _skipped("target_kind_mismatch", _SCALE, "StatefulSet")

    assert await bridge.run_bridge_for_incident(_incident(_STS_POD, "squad-7-k5")) == []
    enrich.assert_not_awaited()
    run.assert_not_awaited()
    k8s.assert_not_awaited()
    assert _skipped("target_kind_mismatch", _SCALE, "StatefulSet") == before + 1

    enrich.return_value = _ctx()
    await bridge.run_bridge_for_incident(_incident(_DEPLOY_POD, "squad-7-k5"))
    enrich.assert_awaited_once()
    run.assert_awaited_once()
