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


# --- наблюдения медика ------------------------------------------------------

# Обезличенная копия формы реального события: наблюдения вперемешку с
# выводами. Выводы («retention снёс», «из-за», «вероятно») в вход модели
# попасть не должны — ни дословно, ни шаблоном.
_MEDIC_EVENT = {
    "summary": (
        "Применены фиксы orleans-zombie (verified). Осталось: 3 сервиса shared "
        "(svc-a, svc-b, svc-c) в ImagePullBackOff — образов нет в реестре "
        "(retention снёс теги), и 2 bot-service в CreateContainerConfigError "
        "из-за отсутствующих ключей в secret/infrastructure: BOT_*, MODERATION_*, "
        "OPENROUTER_API_KEY. Поды перезапускались 88 рестартов."
    ),
    "applied": [
        "orleans-zombie: kingdom/town-db - DELETE status=6 + rollout restart, verified=true",
        "grants: shared config-db - applied but INEFFECTIVE",
    ],
    "manual": ["migration svc-d: schema_migrations dirty, version 20260801120000"],
    "gaps": [],
    "extras": None,
    "root_cause": (
        "Сборки сервисов shared задеплоены тегами, которых нет в реестре "
        "(retention Nexus снёс образы), из-за чего поды в ImagePullBackOff; "
        "bot-service не создаёт контейнер — вероятно, из-за отсутствующих ключей в secret"
    ),
    "next_action": "пересобрать svc-a через teamcity",
}


def test_medic_observations_are_templated_whitelist():
    facts = lrd.extract_medic_observations(_MEDIC_EVENT)
    text = "\n".join(facts)
    assert "состояние подов: CreateContainerConfigError, ImagePullBackOff" in facts
    assert "рестартов контейнера: до 88" in facts
    assert "schema_migrations: dirty=true (версия 20260801120000)" in facts
    assert "Orleans membership: есть записи мёртвых силосов (status=6)" in facts
    keys = next(f for f in facts if f.startswith("в Secret нет ключей"))
    assert {"BOT_*", "MODERATION_*", "OPENROUTER_API_KEY"} <= set(keys.split(": ")[1].split(", "))
    # выводы, имена сервисов и план медика в вход не попадают
    for banned in ("retention", "реестр", "из-за", "вероятно", "teamcity", "svc-a",
                   "Nexus", "пересобрать", "INEFFECTIVE", "grants"):
        assert banned.lower() not in text.lower(), banned


def test_root_cause_and_next_action_are_never_read():
    only_answer = {"root_cause": "поды в CrashLoopBackOff, exit code 137, OOMKilled",
                   "next_action": "ImagePullBackOff dirty BOT_KEY"}
    assert lrd.extract_medic_observations(only_answer) == []


def test_no_root_cause_ngram_leaks_into_facts():
    facts = lrd.extract_medic_observations(_MEDIC_EVENT)
    assert lrd.answer_leak(facts, _MEDIC_EVENT["root_cause"]) == []
    # сама страховка работает: подсунутый кусок вывода ловится
    assert lrd.answer_leak(facts + ["теги которых нет в реестре"],
                           _MEDIC_EVENT["root_cause"])


def test_observations_feed_rules_as_partial_sources():
    facts = ["состояние подов: CrashLoopBackOff, OOMKilled", "код выхода контейнера: 137"]
    ctx = lrd.apply_medic_observations({"source_status": {}}, facts)
    assert {e["reason"] for e in ctx["k8s_events"]} == {"BackOff", "OOMKilled"}
    assert ctx["source_status"]["k8s_pod_state"].startswith("partial")
    assert "код выхода контейнера: 137" in ctx["k8s_summary"]
    summary = lrd.case_summary("Alert in ns", facts)
    assert "без выводов" in summary and "- код выхода контейнера: 137" in summary
    assert lrd.case_summary("Alert in ns", []) == "Alert in ns"


def _run_args(tmp_path, context="auto"):
    import argparse
    return argparse.Namespace(out=str(tmp_path), ids="", limit=100, context=context)


def test_auto_runs_both_modes_on_the_same_cases(tmp_path, monkeypatch):
    import asyncio
    import json
    (tmp_path / "cases.jsonl").write_text(
        json.dumps({"event_id": 1, "observed_medic": ["состояние подов: OOMKilled"]}) + "\n"
        + json.dumps({"event_id": 2, "observed_medic": []}) + "\n")
    seen = []

    async def fake(case, context):
        seen.append((case["event_id"], context))
        return {"event_id": case["event_id"], "context": context}

    monkeypatch.setattr(lrd, "_run_case", fake)
    asyncio.run(lrd._run_async(_run_args(tmp_path)))
    assert sorted(seen) == [(1, "alert_only"), (1, "medic_observed"), (2, "alert_only")]
    seen.clear()
    asyncio.run(lrd._run_async(_run_args(tmp_path)))
    assert seen == []  # повтор ничего не перегоняет


def test_score_is_split_by_context():
    cases = [{"event_id": 1, "expected_primary": "image_missing"}]
    results = [
        {"event_id": 1, "best_cause": None, "ranked_causes": [], "latency_s": 20},
        {"event_id": 1, "context": "medic_observed",
         "best_cause": "ImagePullBackOff: tag not found in registry",
         "ranked_causes": ["ImagePullBackOff: tag not found in registry"], "latency_s": 600},
    ]
    s = lrd.score(cases, results)
    assert s["by_context"]["alert_only"]["abstention_rate"] == 1.0
    assert s["by_context"]["medic_observed"]["top1"] == 1.0
    assert s["cases_run"] == 2
