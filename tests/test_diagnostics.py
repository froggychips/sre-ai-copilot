"""Тесты на deterministic diagnostics-слой.

Принцип: правила — это контракт, поэтому проверяем:
  1. Каждое правило выдаёт хотя бы один Fact (observed=True или False).
  2. На «положительном» контексте — observed=True.
  3. На «чистом» контексте — observed=False.
  4. evidence содержит ожидаемые поля.
  5. DiagnosticEngine не падает, если одно правило выкинуло exception.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.diagnostics import DiagnosticEngine, default_engine
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.diagnostics.rules import (
    CrashLoopBackOffRule,
    FailedSchedulingRule,
    OOMKilledRule,
    RecentDeployRule,
    ResourcePressureRule,
    UpstreamDegradedRule,
)


# ---------- OOMKilledRule -------------------------------------------------

def test_oom_rule_detects_hard_pattern():
    facts = OOMKilledRule().evaluate({
        "description": "container payment-svc was OOMKilled, exit code 137",
        "pod": "payment-svc-7",
    })
    assert len(facts) == 1
    f = facts[0]
    assert f.kind == FactKind.OOM_KILLED
    assert f.observed is True
    assert f.confidence >= 0.9
    assert f.subject == "payment-svc-7"


def test_oom_rule_soft_pattern_low_confidence():
    facts = OOMKilledRule().evaluate({
        "description": "pod was terminated with exit code 137",
    })
    assert facts[0].observed is True
    assert facts[0].confidence < 0.5  # exit 137 без OOMKilled — слабый


def test_oom_rule_clean_context():
    facts = OOMKilledRule().evaluate({
        "description": "pod restarted normally after rolling update",
    })
    assert facts[0].observed is False
    assert facts[0].confidence >= 0.8


# ---------- CrashLoopBackOffRule -----------------------------------------

def test_crashloop_rule_detects_phrase():
    facts = CrashLoopBackOffRule().evaluate({
        "description": "Pod stub-pod-1 CrashLoopBackOff",
    })
    assert facts[0].observed is True
    assert facts[0].confidence >= 0.85


def test_crashloop_rule_detects_high_restart_count():
    facts = CrashLoopBackOffRule().evaluate({
        "k8s_summary": "Container restart count: 12",
    })
    assert facts[0].observed is True
    assert facts[0].evidence["max_restart_count"] == 12


def test_crashloop_rule_clean():
    facts = CrashLoopBackOffRule().evaluate({"description": "p99 latency above 800ms"})
    assert facts[0].observed is False


# ---------- FailedSchedulingRule -----------------------------------------

def test_failed_scheduling_insufficient_cpu():
    facts = FailedSchedulingRule().evaluate({
        "description": "FailedScheduling: 0/5 nodes are available, insufficient cpu",
    })
    assert facts[0].observed is True
    assert facts[0].evidence["cause"] == "insufficient_cpu"


def test_failed_scheduling_taint():
    facts = FailedSchedulingRule().evaluate({
        "k8s_summary": "0/3 nodes are available: untolerated taint",
    })
    assert facts[0].observed is True
    assert facts[0].evidence["cause"] == "taint_mismatch"


def test_failed_scheduling_clean():
    facts = FailedSchedulingRule().evaluate({"description": "OOMKilled"})
    assert facts[0].observed is False


# ---------- RecentDeployRule ---------------------------------------------

def test_recent_deploy_within_window():
    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    deploy_at = incident_at - timedelta(minutes=15)
    facts = RecentDeployRule().evaluate({
        "incident_starts_at": incident_at,
        "recent_deployments": [
            {"name": "town-service", "ts": deploy_at, "sha": "abc1234"}
        ],
        "service": "town-service",
    })
    assert facts[0].observed is True
    assert facts[0].evidence["deploys"][0]["minutes_before_incident"] == 15
    assert facts[0].evidence["deploys"][0]["name"] == "town-service"


def test_recent_deploy_outside_window():
    incident_at = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    deploy_at = incident_at - timedelta(hours=8)
    facts = RecentDeployRule().evaluate({
        "incident_starts_at": incident_at,
        "recent_deployments": [{"name": "x", "ts": deploy_at}],
        "service": "x",
    })
    assert facts[0].observed is False
    assert facts[0].evidence["deploys_checked"] == 1


def test_recent_deploy_iso_string_timestamps():
    """Принимает ISO-строки (alertmanager отдаёт именно их)."""
    facts = RecentDeployRule().evaluate({
        "incident_starts_at": "2026-05-12T10:00:00Z",
        "recent_deployments": [
            {"name": "svc", "ts": "2026-05-12T09:30:00Z"}
        ],
    })
    assert facts[0].observed is True


def test_recent_deploy_no_data():
    facts = RecentDeployRule().evaluate({})
    assert facts[0].observed is False
    assert facts[0].evidence["reason"] == "no_deploys_or_no_timestamp"


# ---------- ResourcePressureRule -----------------------------------------

def test_resource_pressure_text():
    facts = ResourcePressureRule().evaluate({
        "k8s_summary": "Node has MemoryPressure condition true",
    })
    assert facts[0].observed is True
    assert "memory" in facts[0].evidence["types"]


def test_resource_pressure_metrics():
    facts = ResourcePressureRule().evaluate({
        "metrics_summary": {"cpu_pressure": True},
    })
    assert facts[0].observed is True
    assert "cpu" in facts[0].evidence["types"]


def test_resource_pressure_clean():
    facts = ResourcePressureRule().evaluate({"description": "OOMKilled"})
    assert facts[0].observed is False


# ---------- UpstreamDegradedRule -----------------------------------------

def test_upstream_no_graph_data():
    facts = UpstreamDegradedRule().evaluate({"service": "town-service"})
    assert facts[0].observed is False
    assert facts[0].evidence["reason"] == "no_graph_data"
    assert facts[0].confidence < 0.5  # явный low — мы просто не проверяли


def test_upstream_alerts_present():
    facts = UpstreamDegradedRule().evaluate({
        "service": "town-service",
        "upstream_alerts": [
            {"service": "auth", "alertname": "HighLatency", "minutes_before": 3}
        ],
    })
    assert facts[0].observed is True
    assert facts[0].evidence["count"] == 1


def test_upstream_empty_list_means_checked_and_clean():
    """observed=False, но confidence высокий (мы проверили, ничего нет)."""
    facts = UpstreamDegradedRule().evaluate({
        "service": "x",
        "upstream_alerts": [],
    })
    assert facts[0].observed is False
    assert facts[0].confidence >= 0.85


# ---------- DiagnosticEngine ---------------------------------------------

def test_engine_runs_all_rules():
    store = default_engine.run({
        "description": "OOMKilled and CrashLoopBackOff observed",
        "pod": "x",
    })
    kinds = store.observed_kinds()
    assert FactKind.OOM_KILLED in kinds
    assert FactKind.CRASHLOOP in kinds
    # Остальные правила — should produce observed=False, не падать.
    assert len(store.facts) == 6  # ровно по числу правил, см. DEFAULT_RULES


def test_engine_skips_failing_rule_and_continues():
    class BrokenRule:
        name = "BrokenRule"
        def evaluate(self, ctx):
            raise ValueError("kaboom")

    engine = DiagnosticEngine(rules=[BrokenRule(), OOMKilledRule()])
    store = engine.run({"description": "OOMKilled"})
    # Сломавшееся правило пропущено, OOM всё равно зафиксирован.
    assert store.has_observed(FactKind.OOM_KILLED) is True


def test_engine_skips_non_fact_returns():
    class JunkRule:
        name = "JunkRule"
        def evaluate(self, ctx):
            return ["not a Fact"]  # type: ignore[list-item]

    engine = DiagnosticEngine(rules=[JunkRule()])
    store = engine.run({})
    assert store.facts == []


def test_factstore_to_prompt_context_format():
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95,
             subject="pod-1", evidence={"hits": 2}),
        Fact(kind=FactKind.RECENT_DEPLOY, observed=False, confidence=0.9),
    ])
    out = store.to_prompt_context()
    assert "<facts>" in out and "</facts>" in out
    assert "✓ oom_killed" in out
    assert "✗ recent_deploy" in out
    assert "subject=pod-1" in out


def test_fact_kinds_match_rule_outputs():
    """Все правила выдают только канонические kind-slug-и из FactKind.ALL."""
    store = default_engine.run({
        "description": "OOMKilled exit 137 CrashLoopBackOff FailedScheduling MemoryPressure",
        "service": "x",
        "upstream_alerts": [],
        "recent_deployments": [],
    })
    for f in store.facts:
        assert f.kind in FactKind.ALL, f"unknown kind: {f.kind}"
