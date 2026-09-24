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


# --- наблюдения медика (экстрактор живёт в app, наблюдения — в графе) -------

from app.context import medic_observations as mo  # noqa: E402

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
    facts = mo.extract_medic_observations(_MEDIC_EVENT)
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
    assert mo.extract_medic_observations(only_answer) == []


def test_no_root_cause_ngram_leaks_into_facts():
    facts = mo.extract_medic_observations(_MEDIC_EVENT)
    assert lrd.answer_leak(facts, _MEDIC_EVENT["root_cause"]) == []
    # сама страховка работает: подсунутый кусок вывода ловится
    assert lrd.answer_leak(facts + ["теги которых нет в реестре"],
                           _MEDIC_EVENT["root_cause"])


def _run_args(tmp_path, context="auto"):
    import argparse
    return argparse.Namespace(out=str(tmp_path), ids="", limit=100, context=context)


def test_score_is_split_by_context():
    cases = [{"event_id": 1, "expected_primary": "image_missing"}]
    results = [
        {"event_id": 1, "best_cause": None, "ranked_causes": [], "latency_s": 20},
        {"event_id": 1, "context": "kg",
         "best_cause": "ImagePullBackOff: tag not found in registry",
         "ranked_causes": ["ImagePullBackOff: tag not found in registry"], "latency_s": 600},
    ]
    s = lrd.score(cases, results)
    assert s["by_context"]["alert_only"]["abstention_rate"] == 1.0
    assert s["by_context"]["kg"]["top1"] == 1.0
    assert s["cases_run"] == 2

# --- ctx кейса: тот же build_diagnostics_ctx, что у прода ------------------------

_AS_OF = "2026-09-20T10:00:00+00:00"


def _kgc(pod_events=(), code=(), statics=0, medic=(), jobs=()):
    """Контекст графа в той форме, в какой его хранит кейс (JSON сборщика)."""
    from app.context.kg_incident_context import SCHEMA, select_targets

    kgc = {"schema": SCHEMA, "as_of": _AS_OF, "namespace": "squad-alpha-shared",
           "ns_scope": "squad-alpha-%", "namespaces": ["squad-alpha-shared"],
           "pod_events": list(pod_events), "alerts": [],
           "deployments": {"code": list(code), "rollouts": [], "statics_count": statics},
           "jobs": list(jobs), "incident_history": [], "remediation_history": [],
           "medic_observations": ([{"event_id": 7, "observed_at": _AS_OF,
                                    "namespace": "squad-alpha-shared", "facts": list(medic)}]
                                  if medic else []),
           "logs": [], "sources": {}}
    kgc["targets"] = select_targets(kgc)
    return kgc


_BACKOFF = {"namespace": "squad-alpha-shared", "pod": "bravo-service-7d9f8b6c5d-x2k4q",
            "service": "bravo-service", "type": "Warning", "reason": "BackOff",
            "message": "Back-off restarting failed container", "count": 12,
            "first_seen": "2026-09-20T09:30:00+00:00", "last_seen": "2026-09-20T09:55:00+00:00"}
_DEPLOY = {"namespace": "squad-alpha-shared", "service": "bravo-service", "kind": "code",
           "status": "success", "buildtype_id": "Wo_Backend_BuildAndUpdate",
           "started_at": "2026-09-20T09:40:00+00:00", "finished_at": "2026-09-20T09:45:00+00:00",
           "attribution_scope": "service"}


@pytest.fixture(autouse=True)
def _cli_backend(monkeypatch):
    # Settings валидируется при импорте app.*; LLM в этих тестах не зовётся.
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")


def _kg_case():
    return {
        "event_id": 7, "alert_name": "KubeDeploymentReplicasMismatch",
        "namespace": "squad-alpha-shared", "service_name": "bravo-service", "severity": "warning",
        "opened_at": "2026-09-20T10:00:00", "started_at": _AS_OF,
        "kg_context": _kgc([_BACKOFF], [_DEPLOY], medic=["состояние подов: OOMKilled"]),
    }


def test_available_modes_follow_case_inputs():
    assert lrd.available_modes(_kg_case()) == list(lrd.MODES)
    assert lrd.available_modes({"event_id": 1}) == ["alert_only"]
    no_medic = dict(_kg_case(), kg_context=_kgc([_BACKOFF]))
    assert lrd.available_modes(no_medic) == ["alert_only", "kg"]


def test_alert_only_never_sees_graph_and_answers_unknown_not_absent():
    case = _kg_case()
    ctx = lrd.build_case_ctx(case, "alert_only")
    assert not ctx.get("k8s_events") and not ctx.get("k8s_summary")
    assert ctx["source_status"]["k8s_events"].startswith(lrd.NOT_RECONSTRUCTED)
    v = lrd.rule_facts(case, "alert_only")["by_verdict"]
    # Без пометки источника пустое поле давало уверенное ✗ «OOM не было».
    assert "absent" not in v
    assert "oom_killed" in v["unknown"]


def test_kg_mode_feeds_rules_like_prod():
    case = _kg_case()
    ctx = lrd.build_case_ctx(case, "kg")
    assert ctx["k8s_events"][0]["reason"] == "BackOff"
    assert ctx["recent_deployments"][0]["name"] == "bravo-service"
    assert "[kg_pod_events]" in ctx["k8s_summary"] and "[squad-medic]" in ctx["k8s_summary"]
    assert ctx["source_status"]["k8s_events"].startswith("partial")
    assert {"crashloop", "oom_killed"} <= set(lrd.rule_facts(case, "kg")["observed"])


def test_kg_no_medic_drops_only_the_medic_source():
    case = _kg_case()
    ctx = lrd.build_case_ctx(case, "kg_no_medic")
    assert "squad-medic" not in (ctx.get("k8s_summary") or "")
    assert [e["reason"] for e in ctx["k8s_events"]] == ["BackOff"]
    assert "oom_killed" not in lrd.rule_facts(case, "kg_no_medic")["observed"]


def test_case_prompt_is_the_pipeline_block():
    from app.context.kg_incident_context import kg_context_prompt

    case = _kg_case()
    text = lrd.case_prompt(case, "kg")
    assert kg_context_prompt(case["kg_context"]) in text
    assert "[squad-medic]" in text
    assert "[squad-medic]" not in lrd.case_prompt(case, "kg_no_medic")
    assert lrd.case_prompt(case, "alert_only") == lrd._to_incident(case).summary


def test_foreign_workload_events_never_become_text_facts():
    foreign = dict(_BACKOFF, pod="charlie-worker-5f9c8d7b6c-x1k2m", service="charlie-worker",
                   reason="OOMKilled", message="Container charlie was OOMKilled")
    case = dict(_kg_case(), kg_context=_kgc([foreign]))
    ctx = lrd.build_case_ctx(case, "kg")
    # В тексте чужого OOM нет, а структурированное событие несёт pod_name —
    # PodEventsRule видит, что оно у другого workload-а.
    assert "OOMKilled" not in (ctx.get("k8s_summary") or "")
    assert ctx["k8s_events"][0]["pod_name"] == foreign["pod"]
    assert "oom_killed" not in lrd.rule_facts(case, "kg")["observed"]


def test_snapshot_without_graph_rows_still_has_a_mode():
    case = {"event_id": 9, "context_snapshot": {"schema": "incident_ctx/v1",
                                                  "k8s_pod_state": {"x": 1}}}
    assert "kg" in lrd.available_modes(case)
    assert lrd.build_case_ctx(dict(case, alert_name="A", namespace="squad-alpha"),
                              "kg")["k8s_pod_state"] == {"x": 1}


def test_statics_never_becomes_recent_deploy():
    case = dict(_kg_case(), kg_context=_kgc(statics=12))
    ctx = lrd.build_case_ctx(case, "kg")
    assert not ctx.get("recent_deployments")
    assert ctx["source_status"]["recent_deployments"].startswith(lrd.NOT_RECONSTRUCTED)
    assert "recent_deploy" not in lrd.rule_facts(case, "kg")["observed"]


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        lrd.build_case_ctx(_kg_case(), "medic_observed")


def test_auto_runs_every_available_mode(tmp_path, monkeypatch):
    import asyncio
    import json
    (tmp_path / "cases.jsonl").write_text(json.dumps(_kg_case()) + "\n"
                                          + json.dumps({"event_id": 2}) + "\n")
    seen = []

    async def fake(case, context):
        seen.append((case["event_id"], context))
        return {"event_id": case["event_id"], "context": context}

    monkeypatch.setattr(lrd, "_run_case", fake)
    asyncio.run(lrd._run_async(_run_args(tmp_path)))
    assert sorted(seen) == sorted([(7, m) for m in lrd.MODES] + [(2, "alert_only")])
    seen.clear()
    asyncio.run(lrd._run_async(_run_args(tmp_path)))
    assert seen == []  # повтор ничего не перегоняет


def test_case_scope_is_point_in_time_without_own_conclusion():
    scope = lrd.case_scope(_kg_case())
    assert scope.as_of.isoformat() == _AS_OF
    assert scope.exclude_remediation_ids == (7,) and scope.with_conclusions is False


def _json_copy(x):
    import json
    return json.loads(json.dumps(x))


def test_fetch_kg_contexts_filters_leaked_observations(monkeypatch):
    from app.context import kg_incident_context as kgi

    kgc = _kgc([_BACKOFF], medic=["состояние подов: OOMKilled", "теги которых нет в реестре"])
    monkeypatch.setattr(kgi, "fetch_kg_incident_context", lambda reader, scope: _json_copy(kgc))
    case = {"event_id": 7, "namespace": "squad-alpha-shared", "service_name": "alpha-service",
            "started_at": _AS_OF, "root_cause": "теги которых нет в реестре снёс retention"}
    st = lrd.fetch_kg_contexts(object(), [case])
    assert st["with_kg"] == 1 and st["leaks"] == 1
    facts = case["kg_context"]["medic_observations"][0]["facts"]
    assert facts == ["состояние подов: OOMKilled"]
    assert case["target_workload"] == "bravo-service" and st["retargeted"] == 1


# --- target кейса ---------------------------------------------------------------


def test_incident_is_built_around_primary_target():
    case = dict(_kg_case(), alert_name="KubeDeploymentGenerationMismatch",
                service_name="alpha-service")
    assert lrd.set_case_target(case) is True
    assert case["incident_service"] == "alpha-service"
    inc = lrd._to_incident(case)
    assert inc.labels["service"] == "bravo-service"
    assert inc.namespace == "squad-alpha-shared"


def test_no_graph_keeps_incident_service_unless_noise():
    case = {"event_id": 1, "namespace": "n", "service_name": "alpha-service",
            "alert_name": "KubeContainerWaiting", "kg_context": _kgc()}
    assert lrd.set_case_target(case) is False
    assert lrd._to_incident(case).labels["service"] == "alpha-service"
    noise = dict(case, alert_name="KubeDeploymentGenerationMismatch")
    lrd.set_case_target(noise)
    assert lrd._to_incident(noise).labels["service"] == ""


def test_target_event_share():
    other = dict(_BACKOFF, pod="alpha-service-5c7b9d8f6g-k2m4n")
    case = {"kg_context": _kgc([_BACKOFF, other])}
    assert lrd.target_event_share(case, "bravo-service") == 0.5
    assert lrd.target_event_share({"kg_context": _kgc()}, "x") is None


# --- исход кейса -------------------------------------------------------------


def test_outcome_before_cutover_needs_evidence():
    row = {"started_at": "2026-09-20T10:00:00", "fixed": True, "outcome": "partial",
           "still_unhealthy": True}
    ev = lrd.outcome_evidence(row, None)
    assert ev["confirmed"] is False and ev["fixed_semantics"] == "applied_something"
    quiet = {"observable": True, "bad_events_before": 4, "bad_events_after": 0,
             "alerts_open_after": 0}
    assert lrd.outcome_evidence(row, quiet)["confirmed"] is True
    noisy = dict(quiet, bad_events_after=3)
    assert lrd.outcome_evidence(row, noisy)["kg_quiet"] is False
    early = dict(quiet, observable=False)
    assert lrd.outcome_evidence(row, early)["kg_quiet"] is None
    # Нули без поломки в графе до разбора — нет покрытия, а не тишина.
    uncovered = dict(quiet, bad_events_before=0)
    assert lrd.outcome_evidence(row, uncovered)["kg_quiet"] is None
    assert lrd.outcome_evidence(row, uncovered)["confirmed"] is False


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
    results = [{"event_id": 1, "context": "kg", "best_cause": "образ не найден в registry",
                "ranked_causes": ["образ не найден в registry"]},
               {"event_id": 2, "context": "kg", "best_cause": None, "ranked_causes": []}]
    s = lrd.score(cases, results)
    assert s["by_outcome"]["confirmed"]["kg"]["cases_run"] == 1
    assert s["by_outcome"]["unconfirmed"]["kg"]["abstention_rate"] == 1.0
