"""Rule-based classification — детерминированно, без LLM.

Покрытие:
- stale_failed_job (Job + failed>=1 + active=0 + age>=24h)
- regression_post_deploy (alert<=60min + deploy<=30min)
- memory_pressure (OOMKilled / mem_pct>=90)
- chronic_unowned (orphan + chronic_score>=0.5)
- unknown default + signals_used provenance
"""
from __future__ import annotations

from app.remediation.classifier import Classification, classify


def test_stale_failed_job_match() -> None:
    target = {"kind": "Job", "name": "migrate-x"}
    signals = {"failed_jobs": 3, "active_jobs": 0, "job_age_hours": 48}
    res = classify(target, signals)
    assert res.classification is Classification.STALE_FAILED_JOB
    assert res.rule_id == "R1.stale_failed_job_v1"
    assert res.signals_used["failed_jobs"] == 3
    assert res.signals_used["active_jobs"] == 0
    assert res.signals_used["job_age_hours"] == 48
    assert res.confidence_hint == "strong"


def test_stale_failed_job_requires_24h_age() -> None:
    """job_age_hours < 24 не triggert stale_failed_job."""
    target = {"kind": "Job", "name": "migrate-x"}
    signals = {"failed_jobs": 3, "active_jobs": 0, "job_age_hours": 1}
    res = classify(target, signals)
    assert res.classification is Classification.UNKNOWN


def test_stale_failed_job_requires_failed() -> None:
    target = {"kind": "Job", "name": "migrate-x"}
    signals = {"failed_jobs": 0, "active_jobs": 0, "job_age_hours": 48}
    res = classify(target, signals)
    assert res.classification is Classification.UNKNOWN


def test_regression_post_deploy_match() -> None:
    target = {"kind": "Deployment", "name": "town"}
    signals = {"alert_age_minutes": 5, "recent_deploy_age_minutes": 10}
    res = classify(target, signals)
    assert res.classification is Classification.REGRESSION_POST_DEPLOY
    assert res.rule_id == "R2.regression_post_deploy_v1"


def test_regression_requires_recent_deploy() -> None:
    target = {"kind": "Deployment", "name": "town"}
    signals = {"alert_age_minutes": 5, "recent_deploy_age_minutes": 120}
    res = classify(target, signals)
    assert res.classification is Classification.UNKNOWN


def test_memory_pressure_by_event_reason() -> None:
    target = {"kind": "Pod", "name": "town-abc"}
    signals = {"event_reason": "OOMKilled"}
    res = classify(target, signals)
    assert res.classification is Classification.MEMORY_PRESSURE
    assert res.signals_used["event_reason"] == "oomkilled"


def test_memory_pressure_by_mem_pct() -> None:
    target = {"kind": "Pod", "name": "town-abc"}
    signals = {"mem_pct": 95.0}
    res = classify(target, signals)
    assert res.classification is Classification.MEMORY_PRESSURE
    assert res.signals_used["mem_pct"] == 95.0


def test_chronic_unowned_match() -> None:
    target = {"kind": "Deployment", "name": "orphan-svc"}  # owner_kind absent
    signals = {"chronic_score": 0.7}
    res = classify(target, signals)
    assert res.classification is Classification.CHRONIC_UNOWNED


def test_chronic_unowned_blocked_by_owner() -> None:
    """Сервис с known owner — НЕ chronic_unowned."""
    target = {"kind": "Deployment", "name": "svc", "owner_kind": "Helm"}
    signals = {"chronic_score": 0.9}
    res = classify(target, signals)
    assert res.classification is Classification.UNKNOWN


def test_chronic_unowned_by_stale_class() -> None:
    target = {"kind": "Deployment", "name": "orphan-svc"}
    signals = {"stale_class": "suspicious_stale"}
    res = classify(target, signals)
    assert res.classification is Classification.CHRONIC_UNOWNED


def test_unknown_default() -> None:
    target = {"kind": "Deployment", "name": "town"}
    signals: dict[str, object] = {}
    res = classify(target, signals)
    assert res.classification is Classification.UNKNOWN
    assert res.rule_id == "R0.no_match_v1"
    assert res.confidence_hint == "weak"


def test_rule_priority_specific_before_generic() -> None:
    """stale_failed_job триггерится РАНЬШЕ memory_pressure на тех же fields."""
    target = {"kind": "Job", "name": "migrate-oom"}
    signals = {
        "failed_jobs": 1,
        "active_jobs": 0,
        "job_age_hours": 100,
        "event_reason": "OOMKilled",  # тоже бы триггерился, но позже в порядке
    }
    res = classify(target, signals)
    assert res.classification is Classification.STALE_FAILED_JOB
