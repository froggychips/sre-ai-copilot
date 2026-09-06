"""Evidence-контракт: у факта три исхода — found / absent / unknown.

Сторожевые тесты на то, что «не нашли» и «не смогли проверить» не
схлопываются обратно в один observed=False:

  * каждое правило в DEFAULT_RULES объявляет `sources`;
  * при упавших источниках ни одно правило не отвечает ABSENT;
  * UNKNOWN не опровергает гипотезу у критика;
  * enrich_alert помечает сбои источников в source_status, и правила видят их.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from app.agents.fact_critic import _algorithmic_refutations
from app.agents.models.hypothesis import Hypothesis
from app.diagnostics.engine import DiagnosticEngine
from app.diagnostics.facts import Fact, FactKind, FactStore, Verdict
from app.diagnostics.rules import DEFAULT_RULES
from app.diagnostics.rules.base import Rule
from app.diagnostics.rules.oom import OOMKilledRule
from app.diagnostics.rules.pod_events import PodEventsRule
from app.diagnostics.rules.recent_deploy import RecentDeployRule
from app.diagnostics.rules.upstream_degraded import UpstreamDegradedRule
from app.knowledge_graph.queries import deploy_attribution_scope
from app.models.incident import Incident
from app.services.alert_enrichment import _fact_to_short_text, enrich_alert

_INCIDENT_AT = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)


# ── Fact ──────────────────────────────────────────────────────────────────

def test_verdict_derived_from_observed_for_legacy_constructor():
    assert Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.9).verdict == "found"
    assert Fact(kind=FactKind.OOM_KILLED, observed=False, confidence=0.9).verdict == "absent"


def test_unknown_factory_forces_zero_confidence_and_carries_reason():
    f = Fact.unknown(FactKind.OOM_KILLED, "kg_pod_events недоступен", subject="svc",
                     source_rule="R", provenance="kg_pod_events", window_min=60)
    assert f.is_unknown and not f.observed
    assert f.confidence == 0.0
    assert f.unknown_reason == "kg_pod_events недоступен"
    assert f.evidence["reason"] == "kg_pod_events недоступен"   # совместимость с evidence["reason"]
    assert f.epistemic == "unknown"
    assert f.provenance == "kg_pod_events" and f.window_min == 60


def test_unknown_via_constructor_is_normalised_the_same_way():
    f = Fact(kind=FactKind.CRASHLOOP, observed=True, confidence=0.8,
             verdict=Verdict.UNKNOWN.value, unknown_reason="нет данных")
    assert f.observed is False and f.confidence == 0.0 and f.epistemic == "unknown"


def test_found_requires_observed_and_absent_requires_not_observed():
    with pytest.raises(ValueError):
        Fact(kind=FactKind.OOM_KILLED, observed=False, confidence=0.5, verdict="found")
    with pytest.raises(ValueError):
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.5, verdict="absent")


def test_epistemic_must_come_from_the_shared_vocabulary():
    with pytest.raises(ValueError):
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.5, epistemic="gut_feeling")


# ── FactStore ─────────────────────────────────────────────────────────────

def test_unknown_kinds_excludes_kinds_that_were_also_observed():
    store = FactStore([
        Fact.unknown(FactKind.OOM_KILLED, "источник A упал"),
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.9),
        Fact.unknown(FactKind.RECENT_DEPLOY, "поток стоит"),
    ])
    assert store.unknown_kinds() == {FactKind.RECENT_DEPLOY}
    assert store.observed_kinds() == {FactKind.OOM_KILLED}


def test_prompt_context_renders_three_markers_and_warns_about_unknown():
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.9, epistemic="observed"),
        Fact(kind=FactKind.CRASHLOOP, observed=False, confidence=0.9),
        Fact.unknown(FactKind.RECENT_DEPLOY, "kg_deployments недоступен"),
    ])
    text = store.to_prompt_context()
    assert "✓ oom_killed (conf=0.90) [observed]" in text
    assert "✗ crashloop" in text
    assert "? recent_deploy (unknown: kg_deployments недоступен)" in text
    assert "NOT evidence of absence" in text


def test_prompt_context_has_no_unknown_note_when_everything_was_checked():
    store = FactStore([Fact(kind=FactKind.CRASHLOOP, observed=False, confidence=0.9)])
    assert "NOT evidence of absence" not in store.to_prompt_context()


# ── Контракт над DEFAULT_RULES ────────────────────────────────────────────

def test_every_rule_declares_its_sources():
    for rule in DEFAULT_RULES:
        assert rule.sources, f"{rule.name} не объявило sources — run() не сможет отличить ABSENT от UNKNOWN"


@pytest.mark.parametrize("rule", DEFAULT_RULES, ids=lambda r: r.name)
def test_rule_with_all_sources_failed_never_says_absent(rule: Rule):
    ctx: Dict[str, Any] = {
        "namespace": "ns", "service": "svc", "alertname": "SomethingDown",
        "description": "", "incident_starts_at": _INCIDENT_AT,
        "source_status": {src: "failed: test" for src in rule.sources},
    }
    facts = rule.run(ctx)
    for f in facts:
        assert isinstance(f, Fact)
        assert not f.is_absent, f"{rule.name} ответило ABSENT при упавшем источнике: {f}"
        if not f.observed:
            assert f.is_unknown and f.unknown_reason, f


def test_run_demotes_absent_but_keeps_found():
    class R(Rule):
        name = "R"
        sources = ("x",)

        def evaluate(self, ctx):
            return [
                Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.9, source_rule="R"),
                Fact(kind=FactKind.CRASHLOOP, observed=False, confidence=0.9, source_rule="R",
                     evidence={"checked": 3}, window_min=30),
            ]

    found, unknown = R().run({"source_status": {"x": "db down"}})
    assert found.verdict == "found" and found.confidence == 0.9
    assert unknown.is_unknown
    assert unknown.unknown_reason == "x: db down"
    assert unknown.evidence["demoted_from"] == "absent" and unknown.evidence["checked"] == 3
    assert unknown.window_min == 30


def test_run_without_source_problems_is_identity():
    facts = RecentDeployRule().run({"incident_starts_at": _INCIDENT_AT, "recent_deployments": []})
    assert facts[0].is_absent and facts[0].confidence == 0.95


def test_engine_uses_run_and_demotes():
    store = DiagnosticEngine([OOMKilledRule()]).run({
        "alertname": "X", "description": "", "service": "svc",
        "source_status": {"k8s_summary": "kubectl timeout", "logs_summary": "seq down",
                          "k8s_pod_state": "kubectl timeout"},
    })
    assert store.facts and all(f.is_unknown for f in store.facts)
    assert FactKind.OOM_KILLED in store.unknown_kinds()


# ── RecentDeployRule: привязка деплоя — свидетельство, не константа ───────

def _deploy(scope: str, minutes: int = 15) -> Dict[str, Any]:
    return {"name": "svc", "ts": _INCIDENT_AT - timedelta(minutes=minutes),
            "number": "42", "attribution_scope": scope}


def test_recent_deploy_exact_record_is_observed_095():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("service")],
    })[0]
    assert f.verdict == "found" and f.epistemic == "observed" and f.confidence == 0.95
    assert f.evidence["attribution"] == "service"
    assert f.provenance == "kg_deployments/service"
    assert "note" not in f.evidence


def test_recent_deploy_namespace_broadcast_is_inferred_06():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("namespace")],
    })[0]
    assert f.observed and f.epistemic == "inferred" and f.confidence == 0.6
    assert f.evidence["attribution"] == "namespace"
    assert "not attributed to this service" in f.evidence["note"]


def test_recent_deploy_legacy_record_without_scope_is_inferred():
    d = _deploy("namespace")
    d.pop("attribution_scope")
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT, "recent_deployments": [d],
    })[0]
    assert f.epistemic == "inferred" and f.evidence["deploys"][0]["scope"] == "unknown"


def test_recent_deploy_mixed_records_prefer_exact_and_put_it_first():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("namespace", 5), _deploy("service", 20)],
    })[0]
    assert f.epistemic == "observed" and f.confidence == 0.95
    assert f.evidence["deploys"][0]["scope"] == "service"


def test_recent_deploy_empty_live_source_is_absent_with_window():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT, "recent_deployments": [],
    })[0]
    assert f.is_absent and f.confidence == 0.95 and f.window_min == 60
    assert f.epistemic == "observed" and f.provenance == "kg_deployments"


def test_recent_deploy_failed_source_is_unknown_even_with_deploys_present():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("service")],
        "source_status": {"recent_deployments": "поток деплоев не пополняется: 27.0ч"},
    })[0]
    assert f.is_unknown and "27.0ч" in f.unknown_reason


def test_deploy_attribution_scope_mapping():
    assert deploy_attribution_scope({"namespace_scope": False}) == "service"
    assert deploy_attribution_scope({"namespace_scope": True}) == "namespace"
    assert deploy_attribution_scope({}) == "unknown"


def test_short_text_names_unconfirmed_attribution():
    f = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("namespace")],
    })[0]
    assert "привязка к сервису не подтверждена" in _fact_to_short_text(f)
    exact = RecentDeployRule().evaluate({
        "service": "svc", "incident_starts_at": _INCIDENT_AT,
        "recent_deployments": [_deploy("service")],
    })[0]
    assert "не подтверждена" not in _fact_to_short_text(exact)


# ── UpstreamDegradedRule / PodEventsRule ─────────────────────────────────

def test_upstream_none_is_unknown_and_empty_list_is_absent():
    unknown = UpstreamDegradedRule().evaluate({"service": "svc"})[0]
    assert unknown.is_unknown and unknown.unknown_reason == "no_graph_data"
    absent = UpstreamDegradedRule().evaluate({"service": "svc", "upstream_alerts": []})[0]
    assert absent.is_absent and absent.confidence == 0.9 and absent.epistemic == "observed"


def test_upstream_failed_source_reason_wins_over_generic():
    f = UpstreamDegradedRule().evaluate({
        "service": "svc", "upstream_alerts": [],
        "source_status": {"upstream_alerts": "kg_alerts недоступен: OperationalError"},
    })[0]
    assert f.is_unknown and "OperationalError" in f.unknown_reason


def test_pod_events_empty_live_source_stays_silent():
    assert PodEventsRule().evaluate({"service": "svc", "k8s_events": []}) == []


def test_pod_events_failed_source_emits_unknown_per_kind():
    facts = PodEventsRule().evaluate({
        "service": "svc", "k8s_events": [],
        "source_status": {"k8s_events": "kg_pod_events недоступен: TimeoutError"},
    })
    assert {f.kind for f in facts} == {
        FactKind.OOM_KILLED, FactKind.RESOURCE_PRESSURE,
        FactKind.FAILED_SCHEDULING, FactKind.CRASHLOOP,
    }
    assert all(f.is_unknown and f.provenance == "kg_pod_events" for f in facts)


def test_pod_events_scoped_event_is_observed_with_provenance():
    f = PodEventsRule().evaluate({
        "service": "town-service",
        "k8s_events": [{"reason": "OOMKilling", "object": "town-service-7f8c4b6cdf-h2x9k",
                        "message": "oom", "count": 2}],
    })[0]
    assert f.observed and f.epistemic == "observed" and f.provenance == "k8s_event"


# ── Критик: ? — не опровержение ──────────────────────────────────────────

def _hypothesis(anchors: List[str]) -> Hypothesis:
    return Hypothesis.model_construct(
        cause="regression after deploy", detail="d", anchored_facts=anchors,
        confidence=0.7, perspective="deploy",
    )


def test_critic_refutes_absent_anchor_but_not_unknown_anchor():
    absent_store = FactStore([Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.95)])
    assert any("NOT observed" in r for r in _algorithmic_refutations(_hypothesis(["recent_deploy"]), absent_store))

    unknown_store = FactStore([Fact.unknown(FactKind.RECENT_DEPLOY, "kg_deployments недоступен")])
    assert _algorithmic_refutations(_hypothesis(["recent_deploy"]), unknown_store) == []


# ── enrich_alert: source_status заполняется и доходит до правил ──────────

def _incident() -> Incident:
    return Incident(
        incident_id="fp-evidence", severity="warning", status="firing", summary="test",
        namespace="prod-kingdom1",
        labels={"alertname": "KubeDeploymentReplicasMismatch", "severity": "warning",
                "namespace": "prod-kingdom1", "deployment": "auth-service"},
        annotations={}, starts_at="2026-09-06T10:00:00Z",
    )


def _db(in_kg: bool = True) -> MagicMock:
    db = MagicMock()
    svc_row = None
    if in_kg:
        svc_row = MagicMock()
        svc_row.team_owner = "platform"
        svc_row.synthetic = False
        svc_row.updated_at = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row
    db.query.return_value.filter.return_value.first.return_value = svc_row
    return db


_COMMON = dict(
    nearby_alerts=[], incidents_on=[], _downstream_count_by_kind={}, upstream_of=[],
    recent_pod_events_for=[],
)


def _patched(**overrides):
    """Контекст-менеджер с патчами всех KG-запросов enrich_alert."""
    import contextlib
    stack = contextlib.ExitStack()
    values = {**_COMMON, **overrides}
    for name, val in values.items():
        target = f"app.services.alert_enrichment.{name}"
        if isinstance(val, Exception):
            stack.enter_context(patch(target, side_effect=val))
        else:
            stack.enter_context(patch(target, return_value=val))
    return stack


def _fact(ctx, kind: str) -> Fact:
    return next(f for f in ctx.rule_facts if f.kind == kind)


def test_enrich_marks_failed_deploy_query_and_rule_answers_unknown():
    with _patched(recent_deploys_for=RuntimeError("db down"),
                  deploy_stream_freshness={"stale": False}):
        ctx = enrich_alert(_db(), _incident())
    assert ctx.source_status["recent_deployments"].startswith("kg_deployments недоступен")
    f = _fact(ctx, FactKind.RECENT_DEPLOY)
    assert f.is_unknown and "RuntimeError" in f.unknown_reason


def test_enrich_marks_stale_deploy_stream_in_service_scope():
    with _patched(recent_deploys_for=[],
                  deploy_stream_freshness={"last_at": _INCIDENT_AT - timedelta(hours=27),
                                           "age_hours": 27.0, "stale": True}):
        ctx = enrich_alert(_db(), _incident())
    assert "27.0ч" in ctx.source_status["recent_deployments"]
    assert ctx.deploy_stream["stale"] is True
    assert _fact(ctx, FactKind.RECENT_DEPLOY).is_unknown


def test_enrich_live_empty_deploys_is_absent_not_unknown():
    with _patched(recent_deploys_for=[],
                  deploy_stream_freshness={"last_at": _INCIDENT_AT, "age_hours": 0.1, "stale": False}):
        ctx = enrich_alert(_db(), _incident())
    assert "recent_deployments" not in ctx.source_status
    f = _fact(ctx, FactKind.RECENT_DEPLOY)
    assert f.is_absent and f.confidence == 0.95


def test_enrich_empty_upstream_from_live_graph_is_absent():
    with _patched(recent_deploys_for=[], deploy_stream_freshness={"stale": False}):
        ctx = enrich_alert(_db(), _incident())
    f = _fact(ctx, FactKind.UPSTREAM_DEGRADED)
    assert f.is_absent and f.confidence == 0.9


def test_enrich_failed_upstream_query_is_unknown():
    with _patched(recent_deploys_for=[], deploy_stream_freshness={"stale": False},
                  nearby_alerts=RuntimeError("timeout")):
        ctx = enrich_alert(_db(), _incident())
    assert "upstream_alerts" in ctx.source_status
    assert _fact(ctx, FactKind.UPSTREAM_DEGRADED).is_unknown


def test_enrich_failed_pod_events_query_yields_unknown_kinds():
    with _patched(recent_deploys_for=[], deploy_stream_freshness={"stale": False},
                  recent_pod_events_for=RuntimeError("timeout")):
        ctx = enrich_alert(_db(), _incident())
    assert "k8s_events" in ctx.source_status
    unknown = [f for f in ctx.rule_facts if f.source_rule == "PodEventsRule"]
    assert unknown and all(f.is_unknown for f in unknown)


def test_enrich_service_missing_from_kg_marks_all_three_sources():
    with _patched(recent_deploys_for=[], deploy_stream_freshness={"stale": False}):
        ctx = enrich_alert(_db(in_kg=False), _incident())
    assert ctx.in_kg is False
    for src in ("recent_deployments", "upstream_alerts", "k8s_events"):
        assert "KG" in ctx.source_status[src]
    assert _fact(ctx, FactKind.RECENT_DEPLOY).is_unknown
    assert _fact(ctx, FactKind.UPSTREAM_DEGRADED).is_unknown
