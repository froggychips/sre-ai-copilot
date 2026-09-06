"""Тесты на deterministic diagnostics-слой.

Принцип: правила — это контракт, поэтому проверяем:
  1. Каждое правило выдаёт хотя бы один Fact (observed=True или False).
  2. На «положительном» контексте — observed=True.
  3. На «чистом» контексте — observed=False.
  4. evidence содержит ожидаемые поля.
  5. DiagnosticEngine не падает, если одно правило выкинуло exception.
"""
from datetime import datetime, timedelta, timezone


from app.diagnostics import DiagnosticEngine, default_engine
from app.diagnostics.facts import Fact, FactKind, FactStore, MUTUALLY_EXCLUSIVE_PAIRS
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


def test_oom_rule_structured_oomkilled_reason():
    """k8s_pod_state: reason=OOMKilled → structured fact, conf=0.98."""
    facts = OOMKilledRule().evaluate({
        "pod": "payment-svc-7",
        "k8s_pod_state": {
            "payment-svc-7": {"reason": "OOMKilled", "exit_code": 137, "container": "app"},
        },
    })
    assert facts[0].observed is True
    assert facts[0].confidence == 0.98
    assert facts[0].evidence["source"] == "k8s_terminated_state"


def test_oom_rule_structured_exit137_no_reason():
    """k8s_pod_state: exit 137 без reason → conf=0.55 (kill -9 или OOM)."""
    facts = OOMKilledRule().evaluate({
        "pod": "payment-svc-7",
        "k8s_pod_state": {
            "payment-svc-7": {"reason": "Error", "exit_code": 137, "container": "app"},
        },
    })
    assert facts[0].observed is True
    assert facts[0].confidence == 0.55


def test_oom_rule_non_oom_exit_suppresses_text_match():
    """Exit 139 в k8s_pod_state → observed=False, текстовый 'OOMKilled' подавляется."""
    facts = OOMKilledRule().evaluate({
        "pod": "notificator-abc",
        "description": "Container was OOMKilled in the namespace yesterday",
        "k8s_pod_state": {
            "notificator-abc": {"reason": "Error", "exit_code": 139, "container": "app"},
        },
    })
    assert facts[0].observed is False
    assert facts[0].evidence.get("exit_code") == 139


def test_oom_rule_scans_all_pods_finds_oom_in_namespace():
    """Target-под пересоздан, но в pod_state другой под с OOMKilled → observed=True."""
    facts = OOMKilledRule().evaluate({
        "pod": "notificator-abc-new",
        "k8s_pod_state": {
            "notificator-abc-old": {"reason": "OOMKilled", "exit_code": 137, "container": "app"},
        },
    })
    assert facts[0].observed is True
    assert facts[0].confidence == 0.98
    assert facts[0].subject == "notificator-abc-old"


# ---------- CrashLoopBackOffRule -----------------------------------------

def test_crashloop_rule_detects_phrase():
    facts = CrashLoopBackOffRule().evaluate({
        "description": "Pod stub-pod-1 CrashLoopBackOff",
    })
    assert facts[0].observed is True
    assert facts[0].confidence >= 0.85


def test_crashloop_rule_restart_count_alone_is_not_active_crashloop():
    """Кумулятивный restart_count — это история пода, а не активный crashloop.

    `restart_count` в k8s считается за всю жизнь пода: под с 12 рестартами за
    месяцы работы сейчас может быть полностью здоров. Раньше счётчик сам по
    себе давал observed=True (conf 0.7) и якорил гипотезы про crashloop.
    Счётчик остаётся в evidence — для отладки он полезен.
    """
    facts = CrashLoopBackOffRule().evaluate({
        "k8s_summary": "Container restart count: 12",
    })
    assert facts[0].observed is False
    assert facts[0].evidence["max_restart_count"] == 12


def test_crashloop_rule_detects_recent_backoff_event():
    """Свежий BackOff-event — признак именно активного restart-цикла."""
    facts = CrashLoopBackOffRule().evaluate({
        "k8s_summary": "Container restart count: 12",
        "k8s_events": [
            {"reason": "BackOff", "message": "Back-off restarting failed container"},
        ],
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
    """Без времени инцидента окно не построить — это UNKNOWN, не «не было».

    Раньше: observed=False с conf 0.7 и общим reason
    «no_deploys_or_no_timestamp» — пустой список и отсутствие timestamp
    неразличимы. Пустой список при живом источнике теперь ABSENT
    (см. test_recent_deploy_outside_window), отсутствие времени — UNKNOWN.
    """
    facts = RecentDeployRule().evaluate({})
    assert facts[0].observed is False
    assert facts[0].is_unknown
    assert facts[0].confidence == 0.0
    assert facts[0].evidence["reason"] == "no_incident_timestamp"


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


# ---------- Fact conflict detection --------------------------------------

def test_factstore_no_conflicts_when_only_one_observed():
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.PROCESS_CRASH, observed=False, confidence=0.70),
    ])
    assert store.conflicts() == []


def test_factstore_detects_oom_process_crash_conflict():
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.PROCESS_CRASH, observed=True, confidence=0.97),
    ])
    conflicts = store.conflicts()
    assert len(conflicts) == 1
    kinds = {conflicts[0][0].kind, conflicts[0][1].kind}
    assert kinds == {FactKind.OOM_KILLED, FactKind.PROCESS_CRASH}


def test_engine_caps_confidence_on_conflict():
    """DiagnosticEngine понижает confidence конфликтующих фактов до 0.60."""
    from app.diagnostics.rules.process_crash import ProcessCrashRule
    engine = DiagnosticEngine(rules=[OOMKilledRule(), ProcessCrashRule()])
    # Smoke: на non-OOM exit код OOMKilledRule подавляет text-match,
    # поэтому естественного конфликта здесь нет. Результат не ассертим —
    # реальный кап-сценарий форсируем через прямой FactStore ниже.
    engine.run({
        "pod": "notificator-abc",
        "description": "container OOMKilled",
        "k8s_pod_state": {
            "notificator-abc": {"reason": "Error", "exit_code": 139, "container": "app"},
        },
    })
    conflict_store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.PROCESS_CRASH, observed=True, confidence=0.97),
    ])
    from app.diagnostics.engine import DiagnosticEngine as DE
    DE._apply_conflict_signals(conflict_store)
    oom_fact = conflict_store.by_kind(FactKind.OOM_KILLED)[0]
    crash_fact = conflict_store.by_kind(FactKind.PROCESS_CRASH)[0]
    assert oom_fact.confidence == 0.60
    assert crash_fact.confidence == 0.60
    assert oom_fact.evidence["conflict_with"] == FactKind.PROCESS_CRASH
    assert crash_fact.evidence["conflict_with"] == FactKind.OOM_KILLED


def test_conflict_visible_in_prompt_context():
    """<conflicts> блок появляется в to_prompt_context() при конфликте."""
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
        Fact(kind=FactKind.PROCESS_CRASH, observed=True, confidence=0.97),
    ])
    ctx = store.to_prompt_context()
    assert "<conflicts>" in ctx
    assert "mutually exclusive" in ctx


def test_no_conflict_block_when_clean():
    store = FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])
    ctx = store.to_prompt_context()
    assert "<conflicts>" not in ctx


def test_mutually_exclusive_pairs_covers_oom_and_crash():
    """Гайдрейл: oom_killed ↔ process_crash должны быть в MUTUALLY_EXCLUSIVE_PAIRS."""
    pair_kinds = [set(p) for p in MUTUALLY_EXCLUSIVE_PAIRS]
    assert {FactKind.OOM_KILLED, FactKind.PROCESS_CRASH} in pair_kinds


# ---------- DiagnosticEngine ---------------------------------------------

def test_engine_runs_all_rules():
    store = default_engine.run({
        "description": "OOMKilled and CrashLoopBackOff observed",
        "pod": "x",
    })
    kinds = store.observed_kinds()
    assert FactKind.OOM_KILLED in kinds
    assert FactKind.CRASHLOOP in kinds
    # Каждое правило выдаёт >= 1 Fact (✓ или ✗). PodEventsRule при пустом
    # k8s_events выдаёт 0, поэтому проверяем >= len(DEFAULT_RULES) - 1.
    from app.diagnostics.rules import DEFAULT_RULES
    assert len(store.facts) >= len(DEFAULT_RULES) - 1


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
