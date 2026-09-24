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

# --- ctx кейса по режимам входа ------------------------------------------------

_KG_CASE = {
    "event_id": 7, "alert_name": "KubeDeploymentReplicasMismatch", "namespace": "squad-alpha",
    "service_name": "bravo-service", "severity": "warning", "opened_at": "2026-09-20T10:00:00",
    "observed_medic": ["состояние подов: OOMKilled"],
    "kg_context": {
        "pod_events": [{"pod": "bravo-service-1", "type": "Warning", "reason": "BackOff",
                        "message": "Back-off restarting failed container", "count": 12,
                        "last_seen": "2026-09-20T09:55:00"}],
        "deployments": [{"service": "bravo-service", "status": "success",
                         "started_at": "2026-09-20T09:40:00", "finished_at": "2026-09-20T09:45:00"}],
        "alerts": [],
    },
}


@pytest.fixture(autouse=True)
def _cli_backend(monkeypatch):
    # Settings валидируется при импорте app.*; LLM в этих тестах не зовётся.
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")


def test_available_modes_follow_case_inputs():
    assert lrd.available_modes(_KG_CASE) == list(lrd.MODES)
    assert lrd.available_modes({"event_id": 1}) == ["alert_only"]
    assert lrd.available_modes({"event_id": 1, "observed_medic": ["x"]}) == ["alert_only", "medic_observed"]


def test_alert_only_never_sees_graph_and_answers_unknown_not_absent():
    ctx = lrd.build_case_ctx(_KG_CASE, "alert_only")
    assert not ctx.get("k8s_events") and not ctx.get("k8s_summary")
    assert ctx["source_status"]["k8s_events"].startswith(lrd.NOT_RECONSTRUCTED)
    v = lrd.rule_facts(_KG_CASE, "alert_only")["by_verdict"]
    # Без пометки источника пустое поле давало уверенное ✗ «OOM не было».
    assert "absent" not in v
    assert "oom_killed" in v["unknown"]


def test_kg_events_reach_both_structured_and_text_rules():
    ctx = lrd.build_case_ctx(_KG_CASE, "kg_reconstructed")
    assert ctx["k8s_events"][0]["reason"] == "BackOff"
    assert ctx["recent_deployments"][0]["name"] == "bravo-service"
    assert ctx["k8s_summary"].startswith("[kg_pod_events]")
    assert ctx["source_status"]["k8s_events"].startswith("partial")
    assert "crashloop" in lrd.rule_facts(_KG_CASE, "kg_reconstructed")["observed"]
    # Режим kg_reconstructed наблюдений медика не видит.
    assert "squad-medic" not in ctx["k8s_summary"]


def test_kg_plus_medic_appends_not_overwrites():
    ctx = lrd.build_case_ctx(_KG_CASE, "kg+medic")
    assert [e["reason"] for e in ctx["k8s_events"]] == ["BackOff", "OOMKilled"]
    assert "[kg_pod_events]" in ctx["k8s_summary"] and "[squad-medic]" in ctx["k8s_summary"]
    assert {"crashloop", "oom_killed"} <= set(lrd.rule_facts(_KG_CASE, "kg+medic")["observed"])


def test_foreign_workload_events_never_become_text_facts():
    case = dict(_KG_CASE, observed_medic=[], kg_context={
        "pod_events": [{"pod": "charlie-worker-5f9c-x1", "type": "Warning", "reason": "OOMKilled",
                        "message": "Container charlie was OOMKilled", "count": 3}],
        "deployments": [], "alerts": []})
    ctx = lrd.build_case_ctx(case, "kg_reconstructed")
    # В тексте чужого OOM нет, а структурированное событие несёт pod_name —
    # PodEventsRule видит, что оно у другого workload-а.
    assert not ctx.get("k8s_summary")
    assert ctx["k8s_events"][0]["pod_name"] == "charlie-worker-5f9c-x1"
    assert "oom_killed" not in lrd.rule_facts(case, "kg_reconstructed")["observed"]


def test_snapshot_without_graph_rows_still_has_a_mode():
    case = {"event_id": 9, "context_snapshot": {"schema": "incident_ctx/v1",
                                                  "k8s_pod_state": {"x": 1}}}
    assert "kg_reconstructed" in lrd.available_modes(case)
    assert lrd.build_case_ctx(dict(case, alert_name="A", namespace="squad-alpha"),
                              "kg_reconstructed")["k8s_pod_state"] == {"x": 1}


def test_statics_counter_never_becomes_recent_deploy():
    # Статику SQL в строки деплоев не берёт, только счётчиком: «недавний
    # деплой» по веерной раскатке статики был бы истиной почти всегда.
    case = dict(_KG_CASE, observed_medic=[], kg_context={
        "pod_events": [], "alerts": [], "deployments": [], "statics_rollouts": 12})
    ctx = lrd.build_case_ctx(case, "kg_reconstructed")
    assert not ctx.get("recent_deployments")
    assert ctx["source_status"]["recent_deployments"].startswith(lrd.NOT_RECONSTRUCTED)
    assert "статики" in ctx["description"]
    assert "recent_deploy" not in lrd.rule_facts(case, "kg_reconstructed")["observed"]


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        lrd.build_case_ctx(_KG_CASE, "snapshot")


def test_auto_runs_every_available_mode(tmp_path, monkeypatch):
    import asyncio
    import json
    (tmp_path / "cases.jsonl").write_text(json.dumps(_KG_CASE) + "\n")
    seen = []

    async def fake(case, context):
        seen.append(context)
        return {"event_id": case["event_id"], "context": context}

    monkeypatch.setattr(lrd, "_run_case", fake)
    asyncio.run(lrd._run_async(_run_args(tmp_path)))
    assert seen == list(lrd.MODES)


# --- target кейса: сломанный workload, а не ближайший инцидент ---------------

_T0 = "2026-09-20T10:00:00+00:00"


def _target_case(**kg):
    base = {"pod_events": [], "alerts": [], "deployments": []}
    base.update(kg)
    return {"event_id": 7, "namespace": "squad-9-shared", "service_name": "alpha-service",
            "started_at": _T0, "alert_name": "KubeDeploymentGenerationMismatch",
            "kg_context": base}


@pytest.mark.parametrize("pod,workload", [
    ("bravo-service-7d9f8b6c5d-x2k4q", "bravo-service"),
    ("town-db-0", "town-db"),
    ("migrate-job-7x2kq", "migrate-job"),
    ("backup-29812217-q5z7m", "backup"),
    ("plain", "plain"),
])
def test_workload_of_strips_controller_suffixes(pod, workload):
    assert lrd.workload_of(pod) == workload


def test_broken_workload_wins_over_noise_incident_service():
    """Сервис инцидента (шумовой GenerationMismatch) — не target: target там,
    где плохие события подов."""
    case = _target_case(
        pod_events=[
            {"namespace": "squad-9-kingdom2", "pod": "bravo-service-7d9f8b6c5d-x2k4q",
             "type": "Warning", "reason": "BackOff", "count": 12,
             "last_seen": "2026-09-20T09:55:00+00:00"},
            {"namespace": "squad-9-shared", "pod": "alpha-service-5c7b9d8f6g-k2m4n",
             "type": "Normal", "reason": "Pulled", "count": 1,
             "last_seen": "2026-09-20T09:58:00+00:00"},
        ],
        alerts=[{"namespace": "squad-9-shared", "service": "alpha-service",
                 "alertname": "KubeDeploymentGenerationMismatch",
                 "fired_at": "2026-09-20T09:50:00+00:00"}],
    )
    targets = lrd.select_targets(case)
    assert [t["workload"] for t in targets] == ["bravo-service"]
    assert targets[0]["namespace"] == "squad-9-kingdom2"


def test_generation_mismatch_counts_only_with_code_deploy():
    alert = {"namespace": "squad-9-shared", "service": "alpha-service",
             "alertname": "KubeDeploymentGenerationMismatch",
             "fired_at": "2026-09-20T09:50:00+00:00"}
    assert lrd.select_targets(_target_case(alerts=[alert])) == []
    with_deploy = _target_case(alerts=[alert], deployments=[
        {"namespace": "squad-9-shared", "service": "alpha-service", "kind": "code"}])
    assert [t["workload"] for t in lrd.select_targets(with_deploy)] == ["alpha-service"]


def test_closer_events_rank_higher():
    case = _target_case(pod_events=[
        {"namespace": "n", "pod": "old-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": "2026-09-20T08:00:00+00:00"},
        {"namespace": "n", "pod": "new-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": "2026-09-20T09:58:00+00:00"},
    ])
    assert lrd.select_targets(case)[0]["workload"] == "new-svc"


def test_medic_names_only_boost_known_candidates():
    """Имя из applied медика усиливает кандидата, но нового не рождает."""
    case = _target_case(pod_events=[
        {"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 4,
         "last_seen": _T0},
        {"namespace": "n", "pod": "b-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 5,
         "last_seen": _T0},
    ])
    text = '[["rollout restart a-svc", "restart ghost-svc"], []]'
    targets = lrd.select_targets(case, text)
    assert targets[0]["workload"] == "a-svc" and "medic_applied" in targets[0]["sources"]
    assert "ghost-svc" not in {t["workload"] for t in targets}


def test_probe_failures_during_rollout_do_not_pick_target():
    """Unhealthy у раскатывавшегося workload-а — прогрев, не поломка: рестарты
    grainhost от медика не должны делать его target-ом."""
    case = _target_case(
        pod_events=[
            {"namespace": "n", "pod": "town-grainhost-7d9f8b6c5d-x2k4q", "reason": "Unhealthy",
             "count": 20, "last_seen": _T0},
            {"namespace": "n", "pod": "map-service-7d9f8b6c5d-x2k4q", "reason": "BackOff",
             "count": 2, "last_seen": _T0},
        ],
        deployments=[{"namespace": "n", "service": "town-grainhost", "buildtype_id": "k8s_rollout"}],
    )
    assert [t["workload"] for t in lrd.select_targets(case)] == ["map-service"]


def test_probe_failures_are_soft_without_rollout():
    case = _target_case(pod_events=[
        {"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q", "reason": "Unhealthy", "count": 10,
         "last_seen": _T0},
        {"namespace": "n", "pod": "b-svc-7d9f8b6c5d-x2k4q", "reason": "BackOff", "count": 3,
         "last_seen": _T0},
    ])
    assert lrd.select_targets(case)[0]["workload"] == "b-svc"


def test_metric_source_is_never_a_target():
    case = _target_case(alerts=[{"namespace": "n", "service": "vm-kube-state-metrics",
                                 "alertname": "KubeContainerWaiting", "fired_at": _T0}])
    assert lrd.select_targets(case) == []


def test_medic_does_not_boost_probe_only_candidates():
    case = _target_case(pod_events=[
        {"namespace": "n", "pod": "a-svc-7d9f8b6c5d-x2k4q", "reason": "Unhealthy", "count": 5,
         "last_seen": _T0}])
    t = lrd.select_targets(case, '[["rollout restart a-svc"], []]')[0]
    assert "medic_applied" not in t["sources"]


def test_incident_is_built_around_primary_target():
    case = _target_case(pod_events=[
        {"namespace": "squad-9-kingdom2", "pod": "bravo-service-7d9f8b6c5d-x2k4q",
         "reason": "ImagePullBackOff", "count": 3, "last_seen": _T0}])
    assert lrd.assign_targets([case]) == 1
    assert case["incident_service"] == "alpha-service"
    inc = lrd._to_incident(case)
    assert inc.labels["service"] == "bravo-service"
    assert inc.namespace == "squad-9-kingdom2"


def test_no_graph_keeps_incident_service_unless_noise():
    case = dict(_target_case(), alert_name="KubeContainerWaiting")
    assert lrd.assign_targets([case]) == 0
    assert lrd._to_incident(case).labels["service"] == "alpha-service"
    noise = _target_case()  # GenerationMismatch → сервис инцидента не target
    lrd.assign_targets([noise])
    assert lrd._to_incident(noise).labels["service"] == ""


def test_target_event_share():
    case = _target_case(pod_events=[
        {"pod": "bravo-service-7d9f8b6c5d-x2k4q"}, {"pod": "alpha-service-5c7b9d8f6g-k2m4n"}])
    assert lrd.target_event_share(case, "bravo-service") == 0.5
    assert lrd.target_event_share(_target_case(), "x") is None


# --- исход кейса -------------------------------------------------------------


def test_outcome_before_cutover_needs_evidence():
    row = {"started_at": "2026-09-20T10:00:00", "fixed": True, "outcome": "partial",
           "still_unhealthy": True}
    ev = lrd.outcome_evidence(row, None)
    assert ev["confirmed"] is False and ev["fixed_semantics"] == "applied_something"
    quiet = {"observable": True, "bad_events_after": 0, "alerts_open_after": 0}
    assert lrd.outcome_evidence(row, quiet)["confirmed"] is True
    noisy = {"observable": True, "bad_events_after": 3, "alerts_open_after": 0}
    assert lrd.outcome_evidence(row, noisy)["kg_quiet"] is False
    early = {"observable": False, "bad_events_after": 0, "alerts_open_after": 0}
    assert lrd.outcome_evidence(row, early)["kg_quiet"] is None


def test_outcome_after_cutover_fixed_means_healthy():
    row = {"started_at": "2026-09-24T09:30:00", "fixed": True, "outcome": "partial",
           "still_unhealthy": None}
    ev = lrd.outcome_evidence(row, None)
    assert ev["confirmed"] is True and ev["fixed_semantics"] == "healthy"


def test_medic_saw_healthy_is_confirmed():
    row = {"started_at": "2026-09-20T10:00:00", "fixed": True, "outcome": "fixed",
           "still_unhealthy": False}
    assert lrd.outcome_evidence(row, None)["medic_healthy"] is True


def test_score_is_split_by_outcome():
    cases = [{"event_id": 1, "expected_primary": "image_missing", "outcome_confirmed": True},
             {"event_id": 2, "expected_primary": "image_missing", "outcome_confirmed": False}]
    results = [{"event_id": 1, "context": "kg+medic", "best_cause": "образ не найден в registry",
                "ranked_causes": ["образ не найден в registry"]},
               {"event_id": 2, "context": "kg+medic", "best_cause": None, "ranked_causes": []}]
    s = lrd.score(cases, results)
    assert s["by_outcome"]["confirmed"]["kg+medic"]["cases_run"] == 1
    assert s["by_outcome"]["unconfirmed"]["kg+medic"]["abstention_rate"] == 1.0
