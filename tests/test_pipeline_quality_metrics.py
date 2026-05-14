"""Тесты на pipeline-quality метрики (Grok review #7).

Покрываем track-helpers (pure functions): что counter инкрементируется
с правильными labels. Без real-pipeline-execution — слишком много моков.
"""
from app.observability import ai_metrics


def _counter_value(counter, **labels) -> float:
    """Helper: текущее значение метрики с заданными labels."""
    return counter.labels(**labels)._value.get()


def _hist_count(hist) -> float:
    """Количество observations в Histogram без labels.

    Histogram.collect() возвращает Metric с .samples — список Sample(name,
    labels, value, ...). Ищем sample с name суффиксом '_count'.
    """
    for sample in hist.collect()[0].samples:
        if sample.name.endswith("_count"):
            return float(sample.value)
    return 0.0


# ── track_fact_conflict ─────────────────────────────────────────────────────

def test_track_fact_conflict_normalizes_pair_order():
    """Пара (oom_killed, process_crash) и (process_crash, oom_killed) пишутся
    в один counter — лексический sort."""
    before = _counter_value(
        ai_metrics.PIPELINE_FACT_CONFLICTS, kind_a="oom_killed", kind_b="process_crash"
    )
    ai_metrics.track_fact_conflict("process_crash", "oom_killed")
    ai_metrics.track_fact_conflict("oom_killed", "process_crash")
    after = _counter_value(
        ai_metrics.PIPELINE_FACT_CONFLICTS, kind_a="oom_killed", kind_b="process_crash"
    )
    assert after - before == 2


# ── track_resolution_quality ───────────────────────────────────────────────

def test_track_resolution_quality_labels():
    before_r = _counter_value(ai_metrics.PIPELINE_RESOLUTION_QUALITY, quality="resolved")
    before_u = _counter_value(ai_metrics.PIPELINE_RESOLUTION_QUALITY, quality="unresolved")
    ai_metrics.track_resolution_quality("resolved")
    ai_metrics.track_resolution_quality("resolved")
    ai_metrics.track_resolution_quality("unresolved")
    assert _counter_value(ai_metrics.PIPELINE_RESOLUTION_QUALITY, quality="resolved") - before_r == 2
    assert _counter_value(ai_metrics.PIPELINE_RESOLUTION_QUALITY, quality="unresolved") - before_u == 1


# ── track_execution_intent ──────────────────────────────────────────────────

def test_track_execution_intent_parsed_with_action():
    before = _counter_value(
        ai_metrics.EXECUTION_INTENT_EMITTED, parsed="true", action="restart_deployment"
    )
    ai_metrics.track_execution_intent(parsed=True, action="restart_deployment")
    after = _counter_value(
        ai_metrics.EXECUTION_INTENT_EMITTED, parsed="true", action="restart_deployment"
    )
    assert after - before == 1


def test_track_execution_intent_not_parsed_forces_none_action():
    """Если parsed=False, action игнорируется и пишется 'none'."""
    before = _counter_value(
        ai_metrics.EXECUTION_INTENT_EMITTED, parsed="false", action="none"
    )
    ai_metrics.track_execution_intent(parsed=False, action="restart_deployment")  # action игнор-ся
    after = _counter_value(
        ai_metrics.EXECUTION_INTENT_EMITTED, parsed="false", action="none"
    )
    assert after - before == 1


# ── track_executor_status ──────────────────────────────────────────────────

def test_track_executor_status_each_state():
    states = ["dry_run_ok", "dry_run_failed", "guardrail_blocked", "error", "skipped"]
    before = {s: _counter_value(ai_metrics.EXECUTOR_STATUS, status=s) for s in states}
    for s in states:
        ai_metrics.track_executor_status(s)
    after = {s: _counter_value(ai_metrics.EXECUTOR_STATUS, status=s) for s in states}
    assert all(after[s] - before[s] == 1 for s in states)


# ── track_executor_applied ──────────────────────────────────────────────────

def test_track_executor_applied_success_vs_failure():
    before_t = _counter_value(ai_metrics.EXECUTOR_APPLIED, success="true")
    before_f = _counter_value(ai_metrics.EXECUTOR_APPLIED, success="false")
    ai_metrics.track_executor_applied(True)
    ai_metrics.track_executor_applied(False)
    assert _counter_value(ai_metrics.EXECUTOR_APPLIED, success="true") - before_t == 1
    assert _counter_value(ai_metrics.EXECUTOR_APPLIED, success="false") - before_f == 1


# ── recurrence + flapping ──────────────────────────────────────────────────

def test_track_incident_recurrence_bool_label():
    before_t = _counter_value(ai_metrics.INCIDENT_RECURRENCE, is_recurrence="true")
    before_f = _counter_value(ai_metrics.INCIDENT_RECURRENCE, is_recurrence="false")
    ai_metrics.track_incident_recurrence(True)
    ai_metrics.track_incident_recurrence(False)
    assert _counter_value(ai_metrics.INCIDENT_RECURRENCE, is_recurrence="true") - before_t == 1
    assert _counter_value(ai_metrics.INCIDENT_RECURRENCE, is_recurrence="false") - before_f == 1


def test_track_incident_flapping_increments():
    # Use _value.get() to track raw counter
    before = ai_metrics.INCIDENT_FLAPPING._value.get()
    ai_metrics.track_incident_flapping()
    ai_metrics.track_incident_flapping()
    after = ai_metrics.INCIDENT_FLAPPING._value.get()
    assert after - before == 2


# ── hypotheses_count / survivors_count histograms ──────────────────────────

def test_track_hypotheses_count_observes():
    before = _hist_count(ai_metrics.HYPOTHESES_COUNT_PER_RUN)
    ai_metrics.track_hypotheses_count(5)
    ai_metrics.track_hypotheses_count(3)
    after = _hist_count(ai_metrics.HYPOTHESES_COUNT_PER_RUN)
    assert after - before == 2


def test_track_survivors_count_observes_zero_as_valid():
    """0 survivors — реальная ситуация (no_survivor case), должна попадать в histogram."""
    before = _hist_count(ai_metrics.SURVIVORS_COUNT_PER_RUN)
    ai_metrics.track_survivors_count(0)
    after = _hist_count(ai_metrics.SURVIVORS_COUNT_PER_RUN)
    assert after - before == 1
