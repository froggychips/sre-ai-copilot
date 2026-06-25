"""Тесты fail-safe-поведения RCAExplainer.create_report.

Главная инвариантность: гейт аппрува не должен fail-OPEN. Неузнанная причина
ОБЯЗАНА требовать ручного аппрува, а хрупкие гипотезы (без confidence/evidence)
не должны ронять генерацию репорта.
"""
from app.rca.explainer import RCAExplainer


def test_hypothesis_missing_confidence_and_evidence_does_not_crash():
    """Гипотеза без confidence/evidence → не KeyError, репорт собирается."""
    hyps = [{"name": "Memory Pressure / OOM"}]  # нет confidence, нет evidence
    report = RCAExplainer.create_report("INC-1", hyps)

    assert report["incident_id"] == "INC-1"
    assert report["root_cause"]["confidence"] == 0.0
    assert report["hypotheses"][0]["evidence"] == []
    # OOM узнан даже без confidence → действие предложено.
    assert report["suggested_actions"]


def test_empty_hypotheses_returns_safe_default():
    """Пустой список → безопасный дефолт, не краш, без auto-approve."""
    report = RCAExplainer.create_report("INC-EMPTY", [])
    assert report["summary"] == "No root cause identified."
    assert report["suggested_actions"] == []
    assert report["approval_required"] is False
    assert report["risk_level"] == "LOW"


def test_unknown_root_cause_requires_approval():
    """FAIL-SAFE: неузнанная причина → approval_required=True, риск не LOW."""
    hyps = [{"name": "Some Brand New Failure Mode", "confidence": 0.9, "evidence": ["x"]}]
    report = RCAExplainer.create_report("INC-2", hyps)

    assert report["suggested_actions"] == []
    assert report["approval_required"] is True
    assert report["risk_level"] != "LOW"


def test_known_oom_maps_to_correct_action_and_approval():
    """Известный OOM → MEDIUM-действие, approval_required=True."""
    hyps = [{"name": "Memory Pressure / OOM", "confidence": 0.8, "evidence": ["oomkill"]}]
    report = RCAExplainer.create_report("INC-3", hyps)

    assert len(report["suggested_actions"]) == 1
    assert report["suggested_actions"][0]["risk"] == "MEDIUM"
    assert "kubectl set resources" in report["suggested_actions"][0]["command"]
    assert report["risk_level"] == "MEDIUM"
    assert report["approval_required"] is True


def test_oom_mapped_by_stable_kind_despite_name_drift():
    """Дрейф display-имени, но явный kind → действие всё равно мапится."""
    hyps = [
        {
            "name": "totally different wording",
            "kind": "oom",
            "confidence": 0.7,
            "evidence": [],
        }
    ]
    report = RCAExplainer.create_report("INC-4", hyps)
    assert report["suggested_actions"][0]["risk"] == "MEDIUM"
    assert report["approval_required"] is True


def test_app_runtime_failure_is_safe_restart():
    """Runtime failure → SAFE restart; но причина узнана, риск не задирается."""
    hyps = [
        {
            "name": "Application Runtime Failure",
            "confidence": 0.6,
            "evidence": ["panic"],
        }
    ]
    report = RCAExplainer.create_report("INC-5", hyps)
    assert report["suggested_actions"][0]["risk"] == "SAFE"
    # SAFE-действие не требует аппрува и причина узнана → LOW + no approval.
    assert report["risk_level"] == "LOW"
    assert report["approval_required"] is False


def test_confidence_is_rounded_in_summary():
    """confidence*100 округляется до 1 знака — без float-шума."""
    hyps = [
        {"name": "Memory Pressure / OOM", "confidence": 0.123456, "evidence": []}
    ]
    report = RCAExplainer.create_report("INC-6", hyps)
    # 0.123456 * 100 = 12.3456 → 12.3, без длинного хвоста.
    assert "12.3%" in report["summary"]
    assert "12.34" not in report["summary"]


def test_highest_confidence_hypothesis_is_selected():
    """Root cause = гипотеза с макс. confidence, даже если у других ключ отсутствует."""
    hyps = [
        {"name": "Application Runtime Failure", "evidence": []},  # нет confidence → 0.0
        {"name": "Memory Pressure / OOM", "confidence": 0.5, "evidence": []},
    ]
    report = RCAExplainer.create_report("INC-7", hyps)
    assert report["root_cause"]["name"] == "Memory Pressure / OOM"
