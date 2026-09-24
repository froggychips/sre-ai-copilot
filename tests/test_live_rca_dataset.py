"""Оценка live RCA-датасета: классы причин и метрики — без сети и без БД."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_rca_dataset.py"
_spec = importlib.util.spec_from_file_location("live_rca_dataset", _PATH)
assert _spec and _spec.loader
lrd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lrd)


def test_primary_class_is_the_earliest_mention():
    text = ("Образы сервисов отсутствуют в реестре (retention снёс теги), "
            "из-за этого миграции этих сервисов не запускались")
    assert lrd.primary_class(text) == "image_missing"
    assert {"image_missing", "migration"} <= lrd.classify(text)


def test_english_model_answer_is_classified():
    assert "crash_after_deploy" in lrd.classify(
        "New ReplicaSet enters CrashLoopBackOff: startup probe connection refused on :8080"
    )
    assert "image_missing" in lrd.classify("ImagePullBackOff: image tag not found in registry")


def test_score_top1_top3_abstention():
    cases = [
        {"event_id": 1, "expected_primary": "image_missing"},
        {"event_id": 2, "expected_primary": "migration"},
        {"event_id": 3, "expected_primary": "orleans_membership"},
        {"event_id": 4, "expected_primary": None},
    ]
    results = [
        {"event_id": 1, "best_cause": "ErrImagePull: tag deleted from registry",
         "ranked_causes": ["ErrImagePull: tag deleted from registry"], "latency_s": 10},
        {"event_id": 2, "best_cause": "OOMKilled",
         "ranked_causes": ["OOMKilled", "dirty schema migration"], "latency_s": 20},
        {"event_id": 3, "best_cause": None, "ranked_causes": [], "latency_s": 30},
        {"event_id": 4, "best_cause": "something vague", "ranked_causes": [], "latency_s": 5},
    ]
    s = lrd.score(cases, results)
    assert s["gradable"] == 3
    assert s["top1"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["top3"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["abstention_rate"] == 0.25
    assert s["unclassified_answer_rate"] == 0.25


def test_errors_are_counted_not_scored():
    s = lrd.score([{"event_id": 1, "expected_primary": "migration"}],
                  [{"event_id": 1, "error": "TimeoutError: cli"}])
    assert s["errors"] == 1 and s["gradable"] == 0 and s["top1"] is None


def test_dataset_inside_repo_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        lrd._guard_out_dir(_PATH.parent / "live-rca")
    assert lrd._guard_out_dir(tmp_path / "ok").is_dir()
