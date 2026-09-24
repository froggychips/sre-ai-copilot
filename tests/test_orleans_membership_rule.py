"""OrleansMembershipRule: мёртвые записи силосов — фон, всплеск — находка."""
from __future__ import annotations

from app.diagnostics.engine import DiagnosticEngine
from app.diagnostics.facts import FactKind, FactStore, Verdict
from app.diagnostics.rules import DEFAULT_RULES
from app.diagnostics.rules.orleans_membership import OrleansMembershipRule

BACKGROUND = "Orleans membership: есть записи мёртвых силосов (status=6)"


def _run(**ctx):
    ctx.setdefault("service", "alpha-grainhost")
    return OrleansMembershipRule().run(ctx)


def _one(facts):
    assert len(facts) == 1
    f = facts[0]
    assert f.kind == FactKind.ORLEANS_MEMBERSHIP_DEGRADED
    return f


def test_rule_is_registered():
    assert any(isinstance(r, OrleansMembershipRule) for r in DEFAULT_RULES)


def test_constant_background_is_absent_and_chronic():
    f = _one(_run(k8s_summary=f"состояние подов: CrashLoopBackOff\n{BACKGROUND}"))
    assert f.verdict == Verdict.ABSENT.value
    assert f.evidence["chronic"] is True


def test_english_dead_silo_line_is_background():
    f = _one(_run(logs_summary="membership table has 4 dead silo entries (Status=6)"))
    assert f.verdict == Verdict.ABSENT.value
    assert f.evidence["chronic"] is True


def test_no_orleans_mention_no_fact():
    assert _run(k8s_summary="состояние подов: ImagePullBackOff") == []


def test_dead_without_orleans_is_not_background():
    # «dead» без Orleans/membership — чужой текст, не про силосы.
    assert _run(logs_summary="worker dead letter queue overflow") == []


def test_fresh_text_signal_is_found():
    f = _one(_run(
        k8s_summary=BACKGROUND,
        logs_summary="SyncMapSourceEffects failed: target silo S10.1.2.3:11111 is not active",
    ))
    assert f.verdict == Verdict.FOUND.value
    assert f.evidence["dead_records_mentioned"] is True
    assert f.evidence["fresh_signals"]


def test_fresh_signal_in_k8s_event():
    f = _one(_run(
        k8s_summary=BACKGROUND,
        k8s_events=[{"reason": "Unhealthy", "message": "SiloUnavailableException on startup"}],
    ))
    assert f.verdict == Verdict.FOUND.value


def test_quorum_only_counts_in_orleans_line():
    # Кворум Postgres — не про membership: остаётся фоном.
    f = _one(_run(k8s_summary=f"{BACKGROUND}\npostgres synchronous quorum lost"))
    assert f.verdict == Verdict.ABSENT.value
    f = _one(_run(k8s_summary=f"{BACKGROUND}\nOrleans cluster lost quorum"))
    assert f.verdict == Verdict.FOUND.value


def test_growth_of_dead_records_is_found():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_membership={"dead_now": 9, "dead_before": 3}))
    assert f.verdict == Verdict.FOUND.value
    assert f.evidence["dead_records_growth"] == {"dead_now": 9, "dead_before": 3}


def test_same_count_is_still_background():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_membership={"dead_now": 5, "dead_before": 5}))
    assert f.verdict == Verdict.ABSENT.value


def test_bool_counts_are_ignored():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_membership={"dead_now": True, "dead_before": False}))
    assert f.verdict == Verdict.ABSENT.value


def _health(latest, baseline, deltas):
    return {"present": True, "latest": latest, "baseline": baseline, "deltas_pct": deltas}


def test_health_spike_is_found():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_health=_health(
        {"orleans_pings_missed_rate": 4.0}, {"orleans_pings_missed_rate": 1.0},
        {"orleans_pings_missed_rate": 300.0},
    )))
    assert f.verdict == Verdict.FOUND.value
    assert "orleans_pings_missed_rate" in f.evidence["health_spikes"]


def test_health_steady_high_is_background():
    # Высоко, но как всегда: суточная база та же — это фон, не всплеск.
    f = _one(_run(k8s_summary=BACKGROUND, orleans_health=_health(
        {"orleans_messaging_fault_rate": 5.0}, {"orleans_messaging_fault_rate": 4.8},
        {"orleans_messaging_fault_rate": 4.0},
    )))
    assert f.verdict == Verdict.ABSENT.value


def test_health_below_noise_floor_is_ignored():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_health=_health(
        {"orleans_timedout_rate": 0.1}, {"orleans_timedout_rate": 0.0},
        {"orleans_timedout_rate": 1000.0},
    )))
    assert f.verdict == Verdict.ABSENT.value


def test_health_from_zero_baseline_above_floor_is_found():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_health=_health(
        {"orleans_timedout_rate": 2.0}, {"orleans_timedout_rate": 0.0}, {},
    )))
    assert f.verdict == Verdict.FOUND.value


def test_failed_logs_source_demotes_background_to_unknown():
    f = _one(_run(k8s_summary=BACKGROUND, source_status={"logs_summary": "failed: timeout"}))
    assert f.verdict == Verdict.UNKNOWN.value


def test_prompt_marks_background():
    store = FactStore(_run(k8s_summary=BACKGROUND))
    text = store.to_prompt_context()
    assert "[фон, наблюдается постоянно]" in text
    assert "NOT a root cause by itself" in text


def test_prompt_has_no_background_note_for_found():
    store = FactStore(_run(k8s_summary=BACKGROUND, logs_summary="silo S1 is not active"))
    assert "[фон, наблюдается постоянно]" not in store.to_prompt_context()


def test_engine_keeps_background_out_of_observed_kinds():
    store = DiagnosticEngine().run({"service": "alpha-grainhost", "k8s_summary": BACKGROUND})
    assert FactKind.ORLEANS_MEMBERSHIP_DEGRADED not in store.observed_kinds()


def test_foreign_workload_event_is_ignored():
    # SiloUnavailable соседнего grainhost-а — не причина этого инцидента.
    f = _one(_run(
        k8s_summary=BACKGROUND,
        k8s_events=[{"reason": "Unhealthy", "object": "bravo-grainhost-5d8f9-xk2lp",
                     "message": "SiloUnavailableException"}],
    ))
    assert f.verdict == Verdict.ABSENT.value
    assert f.evidence["chronic"] is True


def test_scoped_workload_event_is_strong():
    f = _one(_run(
        k8s_summary=BACKGROUND,
        k8s_events=[{"reason": "Unhealthy", "object": "alpha-grainhost-5d8f9-xk2lp",
                     "message": "SiloUnavailableException"}],
    ))
    assert f.verdict == Verdict.FOUND.value
    assert f.confidence == 0.8


def test_unverified_event_is_weak():
    f = _one(_run(
        k8s_summary=BACKGROUND,
        k8s_events=[{"reason": "Unhealthy", "message": "silo S1 is not active"}],
    ))
    assert f.verdict == Verdict.FOUND.value
    assert f.confidence < 0.5
    assert f.evidence["unverified_events"] == 1


def test_missing_health_baseline_is_not_growth_from_zero():
    f = _one(_run(k8s_summary=BACKGROUND, orleans_health=_health(
        {"orleans_timedout_rate": 2.0}, {"orleans_timedout_rate": None},
        {"orleans_timedout_rate": None},
    )))
    assert f.verdict == Verdict.ABSENT.value


def test_failed_health_source_demotes_background_to_unknown():
    f = _one(_run(k8s_summary=BACKGROUND, source_status={"orleans_health": "failed: VMQueryError"}))
    assert f.verdict == Verdict.UNKNOWN.value
