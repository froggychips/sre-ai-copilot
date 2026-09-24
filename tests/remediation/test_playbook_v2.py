"""Playbook v2: схема, матчер по фактам, рендер плана через DSL, привязка в gate.

Покрытие:
- v2 валидирует на загрузке: действие ∈ ActionType, fact ∈ FactKind,
  verify ∈ VERIFY_CHECKS; поля v1/v2 не смешиваются;
- preconditions fail-closed: UNKNOWN не удовлетворяет ни found, ни absent;
- render_plan собирает argv только через DSLTranslator и валидацию intent-а;
- gate-политика в YAML, вне кандидатов; действие вне её plan → BLOCK;
- REMEDIATION_PLAYBOOK_BINDING_ENABLED: выключен — поведение прежнее,
  включён — мутирующий intent обязан ссылаться на playbook и его план.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.core.execution_dsl import DSLTranslator, ExecutionIntent
from app.diagnostics.facts import Fact, FactStore
from app.remediation import executor_gate, matcher
from app.remediation import verification as v
from app.remediation.executor_gate import PolicyMode, evaluate_intent_gate
from app.remediation.matcher import (PlanRenderError, check_preconditions,
                                     match_playbooks, render_plan)
from app.remediation.playbook import (Playbook, PlaybookValidationError,
                                      load_playbook, load_registry)
from app.remediation.verify_checks import VERIFY_CHECKS, evaluate_verify
from app.services.intent_signature import compute_signature

_RESTART = "restart_crashloop_deployment"


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "pb.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _v2(**overrides) -> dict:
    data = {
        "schema_version": "remediation.playbook/v2",
        "name": "t_restart",
        "kind": "remediation",
        "match": {"alertnames": ["KubePodCrashLooping"]},
        "preconditions": [{"fact": "crashloop", "verdict": "found"}],
        "policy": {
            "approve": {"namespace_tier": ["dev", "squad"]},
            "block": {"any": {"namespace_tier": ["prod", "system"]}},
        },
        "plan": {"steps": [{"action": "restart_deployment"}]},
    }
    data.update(overrides)
    return data


def _fact(kind: str, verdict: str, **evidence) -> Fact:
    if verdict == "unknown":
        return Fact.unknown(kind, "source down", evidence=evidence)
    return Fact(kind=kind, observed=verdict == "found", confidence=0.9,
                verdict=verdict, evidence=dict(evidence))


def default_registry_pb(name: str) -> Playbook:
    return load_registry()[name]


def _intent(namespace: str = "squad-1", action: str = "restart_deployment",
            **kw) -> ExecutionIntent:
    data = {
        "action": action, "resource_type": "deployment",
        "resource_name": "town-service", "namespace": namespace,
        "params": kw.pop("params", {}), "risk": "low",
    }
    data.update(kw)
    return ExecutionIntent.model_validate(data)


# ── схема ────────────────────────────────────────────────────────────────

def test_real_restart_playbook_is_v2_and_executable() -> None:
    pb = load_registry()[_RESTART]
    assert pb.executable
    assert pb.step_actions() == {"describe_resource", "restart_deployment"}
    assert set(pb.verify or ()) <= set(VERIFY_CHECKS)


def test_v1_sample_stays_preview_only() -> None:
    pb = load_registry()["cleanup_stale_failed_job"]
    assert not pb.executable
    assert pb.step_actions() == frozenset()


@pytest.mark.parametrize("patch_, needle", [
    ({"plan": {"steps": [{"action": "delete_namespace"}]}}, "unknown action"),
    ({"preconditions": [{"fact": "oom_kiled", "verdict": "absent"}]}, "unknown fact"),
    ({"preconditions": [{"fact": "crashloop", "verdict": "unknown"}]}, "verdict"),
    ({"verify": ["rollout_converged"]}, "unknown verify check"),
    ({"plan": {"steps": [{"action": "restart_deployment"}],
               "command": ["kubectl", "delete", "ns", "x"]}}, "forbidden"),
    ({"plan": {"steps": []}}, "plan.steps"),
    ({"match": {}}, "classification or non-empty alertnames"),
    ({"preconditions": [{"fact": "crashloop", "verdict": "absent",
                         "evidence_not_in": {"exit_code": [139]}}]}, "only to verdict: found"),
    ({"plan": {"steps": [{"action": "restart_deployment", "cmd": "x"}]}}, "extra"),
])
def test_v2_schema_rejects(patch_, needle) -> None:
    with pytest.raises(Exception) as exc:
        Playbook.model_validate(_v2(**patch_))
    assert needle.lower() in str(exc.value).lower()


def test_v1_rejects_v2_fields(tmp_path) -> None:
    path = _write(tmp_path, """
schema_version: remediation.playbook/v1
name: foo
kind: remediation
match: {classification: stale_failed_job}
policy: {}
plan:
  command: ["kubectl", "get", "pods"]
  steps: [{action: get_pods}]
""")
    with pytest.raises(PlaybookValidationError, match="v1 does not support"):
        load_playbook(path)


def test_unquoted_yaml_no_is_rejected_not_coerced(tmp_path) -> None:
    """`data_plane: [no]` PyYAML читает как [False] — схема обязана упасть,
    а не превратить «нет data-plane» в непонятное значение."""
    path = _write(tmp_path, """
schema_version: remediation.playbook/v2
name: foo
kind: remediation
match: {alertnames: [KubePodCrashLooping]}
policy:
  approve: {namespace_tier: [squad], data_plane: [no]}
plan:
  steps: [{action: restart_deployment}]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


# ── preconditions ────────────────────────────────────────────────────────

@pytest.mark.parametrize("facts, expected, ok", [
    ([("crashloop", "found")], "found", True),
    ([("crashloop", "unknown")], "found", False),
    ([], "found", False),
    ([("oom_killed", "absent")], "absent", True),
    # Один источник «не нашёл», другой упал — это пробел, не отсутствие.
    ([("oom_killed", "absent"), ("oom_killed", "unknown")], "absent", False),
    ([("oom_killed", "unknown")], "absent", False),
    ([("oom_killed", "found"), ("oom_killed", "absent")], "absent", False),
    ([], "absent", False),
])
def test_preconditions_fail_closed(facts, expected, ok) -> None:
    kind = "crashloop" if expected == "found" else "oom_killed"
    pb = Playbook.model_validate(_v2(preconditions=[{"fact": kind, "verdict": expected}]))
    store = FactStore([_fact(k, verdict) for k, verdict in facts])
    assert check_preconditions(pb, store)[0] is ok


@pytest.mark.parametrize("evidence, ok", [
    ({"exit_code": 1}, True),
    ({"exit_code": 139}, False),
    ({}, False),  # ключа нет — не доказано, что это не сигнал
])
def test_evidence_not_in(evidence, ok) -> None:
    pb = Playbook.model_validate(_v2(preconditions=[{
        "fact": "process_crash", "verdict": "found",
        "evidence_not_in": {"exit_code": [134, 139]},
    }]))
    store = FactStore([_fact("process_crash", "found", **evidence)])
    assert check_preconditions(pb, store)[0] is ok


def test_match_playbooks_filters_alertname_v1_and_preconditions() -> None:
    registry = {
        "b": Playbook.model_validate(_v2(name="b")),
        "a": Playbook.model_validate(_v2(name="a")),
        "other_alert": Playbook.model_validate(
            _v2(name="other_alert", match={"alertnames": ["KubeNodeNotReady"]})),
        "v1": load_registry()["cleanup_stale_failed_job"],
    }
    store = FactStore([_fact("crashloop", "found")])
    names = [pb.name for pb in match_playbooks(
        registry, alertname="KubePodCrashLooping", facts=store)]
    assert names == ["a", "b"]
    assert match_playbooks(registry, alertname="KubePodCrashLooping",
                           facts=FactStore()) == []


def test_gate_policy_is_not_a_candidate() -> None:
    assert "_executor_apply_gate" not in load_registry()
    assert executor_gate._EXECUTOR_GATE_POLICY.executable


# ── рендер плана ─────────────────────────────────────────────────────────

def test_render_plan_goes_through_dsl() -> None:
    pb = load_registry()[_RESTART]
    rendered = render_plan(pb, namespace="squad-3", resource_name="worker")
    assert [argv for _, argv in rendered] == [
        ["kubectl", "describe", "deployment/worker", "-n", "squad-3"],
        ["kubectl", "rollout", "restart", "deployment/worker", "-n", "squad-3"],
    ]
    for intent, argv in rendered:
        assert intent.playbook == _RESTART
        assert argv == DSLTranslator.to_argv(intent)


def test_render_plan_templates_and_required_params() -> None:
    pb = Playbook.model_validate(_v2(plan={"steps": [
        {"action": "scale_deployment",
         "params": {"replicas": "{replicas}", "current_replicas": "{current_replicas}"}},
    ]}))
    (intent, argv), = render_plan(pb, namespace="squad-3", resource_name="worker",
                                  context={"replicas": 2, "current_replicas": 1})
    assert "--replicas=2" in argv and "--current-replicas=1" in argv
    with pytest.raises(PlanRenderError, match="has no value"):
        render_plan(pb, namespace="squad-3", resource_name="worker")
    bare = Playbook.model_validate(_v2(plan={"steps": [{"action": "scale_deployment"}]}))
    with pytest.raises(PlanRenderError, match="missing required params"):
        render_plan(bare, namespace="squad-3", resource_name="worker")


def test_render_plan_keeps_intent_validation() -> None:
    pb = load_registry()[_RESTART]
    with pytest.raises(PlanRenderError, match="invalid intent"):
        render_plan(pb, namespace="kube-system", resource_name="worker")
    with pytest.raises(PlanRenderError, match="invalid intent"):
        render_plan(pb, namespace="squad-3", resource_name="x --namespace=prod")
    with pytest.raises(PlanRenderError, match="not v2"):
        render_plan(load_registry()["cleanup_stale_failed_job"],
                    namespace="squad-3", resource_name="worker")


# ── verify ───────────────────────────────────────────────────────────────

def test_verify_names_match_live_assess_output() -> None:
    """Имена VERIFY_CHECKS читают реальные ключи assess(): переименование
    там должно ронять этот тест, а не давать вечный None."""
    intent = _intent("squad-3")
    def deploy(generation, image):
        return {
            "metadata": {"name": "town-service", "namespace": "squad-3",
                         "uid": "u1", "generation": generation},
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"observedGeneration": generation, "readyReplicas": 2},
        }
    before = v.parse_snapshot(deploy(5, "a"), intent).to_dict()
    now = v.parse_snapshot(deploy(6, "b"), intent)
    result = v.assess(intent=intent, before=before, now_snap=now, alert_resolved=True,
                      new_crash_events=0, attempt=1, max_attempts=2)
    assert result["outcome"] == "verified"
    got = evaluate_verify(VERIFY_CHECKS, result["checks"])
    assert got == {name: True for name in VERIFY_CHECKS}
    crashed = dict(result["checks"], new_crash_events=2)
    assert evaluate_verify(["no_new_crash_events"], crashed) == {"no_new_crash_events": False}
    assert evaluate_verify(["converged"], {}) == {"converged": None}


# ── gate ─────────────────────────────────────────────────────────────────

def test_gate_blocks_action_missing_from_gate_plan(monkeypatch) -> None:
    pb = executor_gate._EXECUTOR_GATE_POLICY
    narrowed = pb.model_copy(update={"plan": pb.plan.model_copy(update={
        "steps": [s for s in pb.plan.steps or () if s.action != "restart_deployment"],
    })})
    monkeypatch.setattr(executor_gate, "_EXECUTOR_GATE_POLICY", narrowed)
    decision = evaluate_intent_gate(_intent())
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["rule"] == "block_action_not_in_gate_plan"


@pytest.fixture
def binding_on(monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", True)


def test_binding_off_ignores_playbook_field() -> None:
    assert not settings.REMEDIATION_PLAYBOOK_BINDING_ENABLED
    base = evaluate_intent_gate(_intent())
    assert base.mode == PolicyMode.APPROVE
    assert evaluate_intent_gate(_intent(playbook="no_such_playbook")) == base


def _bound(pb: Playbook, namespace: str = "squad-1", **kw):
    """Intent, привязанный к серверному снимку с одной записью `pb`."""
    from app.remediation.binding import build_match_snapshot
    snap = build_match_snapshot([pb], facts=None, namespace=namespace,
                                alertname="KubePodCrashLooping", classification=None)
    intent = _intent(namespace, playbook=pb.name,
                     playbook_match=snap["entries"][0]["binding"], **kw)
    return intent, snap


@pytest.mark.parametrize("kw, reason", [
    ({}, "playbook_missing"),
    # Имя без серверного снимка — привязки нет, как бы модель ни назвала playbook.
    ({"playbook": _RESTART}, "match_snapshot_missing"),
])
def test_binding_blocks(binding_on, kw, reason) -> None:
    decision = evaluate_intent_gate(_intent(**kw))
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["rule"] == "block_playbook_binding"
    assert decision.reasons[0]["reason"] == reason


def test_binding_allows_bound_restart_and_readonly(binding_on) -> None:
    intent, snap = _bound(default_registry_pb(_RESTART))
    assert evaluate_intent_gate(intent, match_snapshot=snap).mode == PolicyMode.APPROVE
    readonly = _intent(action="describe_resource")
    assert evaluate_intent_gate(readonly).mode == PolicyMode.APPROVE


def test_binding_cannot_loosen_gate(binding_on) -> None:
    """Политика playbook-а складывается по строжайшему: prod остаётся BLOCK
    по gate, даже если бы playbook разрешал."""
    intent, snap = _bound(default_registry_pb(_RESTART), "prod-k1")
    decision = evaluate_intent_gate(intent, match_snapshot=snap)
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["axis"] == "namespace_tier"


def test_binding_applies_stricter_playbook_policy(binding_on, monkeypatch) -> None:
    strict = Playbook.model_validate(_v2(name="t_strict", policy={
        "block": {"any": {"namespace_tier": ["squad"]}},
    }))
    monkeypatch.setattr(matcher, "default_registry", lambda: {"t_strict": strict})
    intent, snap = _bound(strict)
    decision = evaluate_intent_gate(intent, match_snapshot=snap)
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["rule"] == "block.any"


def test_binding_registry_error_blocks(binding_on, monkeypatch) -> None:
    def broken():
        raise PlaybookValidationError("bad yaml")
    monkeypatch.setattr(matcher, "default_registry", broken)
    decision = evaluate_intent_gate(_intent(playbook=_RESTART))
    assert decision.reasons[0]["reason"] == "registry_unavailable"


# ── подпись и FixAgent ───────────────────────────────────────────────────

def test_signature_unchanged_without_playbook() -> None:
    """Подписи уже одобренных intent-ов (без playbook) не должны сдвинуться."""
    plain = _intent()
    assert compute_signature(plain) == compute_signature(replace_playbook(plain, None))
    assert compute_signature(replace_playbook(plain, _RESTART)) != compute_signature(plain)


def replace_playbook(intent: ExecutionIntent, playbook):
    return intent.model_copy(update={"playbook": playbook})


def test_intent_playbook_field_is_charset_checked() -> None:
    with pytest.raises(Exception):
        _intent(playbook="x; rm -rf /")


@pytest.mark.asyncio
async def test_fix_agent_prompt_lists_candidates_only_when_given() -> None:
    from app.agents.fix import FixAgent
    fake = AsyncMock(return_value='{"action": "restart_deployment", '
                     '"resource_type": "deployment", "resource_name": "worker", '
                     '"namespace": "squad-3", "playbook": "%s"}' % _RESTART)
    with patch("app.agents.base.BaseAgent.ask", new=fake):
        _, intent = await FixAgent().suggest("cause", playbooks=[load_registry()[_RESTART]])
        # Список playbook-ов — наш текст, а не данные: в instruction (system).
        instr = fake.await_args.kwargs["instruction"]
        assert "ALLOWED REMEDIATION PLAYBOOKS" in instr and _RESTART in instr
        assert "ALLOWED REMEDIATION PLAYBOOKS" not in fake.await_args.kwargs["user_context"]
        assert intent is not None and intent.playbook == _RESTART
        await FixAgent().suggest("cause")
        assert "ALLOWED REMEDIATION PLAYBOOKS" not in fake.await_args.kwargs["instruction"]


def test_preview_ignores_v2(monkeypatch) -> None:
    """preview матчит только v1: v2 с тем же classification не должен
    попасть в кандидаты и уронить рендер отсутствующего plan.command."""
    from app.remediation.classifier import Classification, ClassificationResult
    from app.remediation.preview import _select_candidate_playbooks
    v2 = Playbook.model_validate(_v2(match={"classification": "stale_failed_job"}))
    cls = ClassificationResult(classification=Classification.STALE_FAILED_JOB, rule_id="t")
    assert _select_candidate_playbooks({"v2": v2}, cls, {}) == []



@pytest.mark.asyncio
async def test_fix_agent_prompt_says_none_when_binding_on_but_no_candidates() -> None:
    from app.agents.fix import FixAgent
    fake = AsyncMock(return_value="{}")
    with patch("app.agents.base.BaseAgent.ask", new=fake):
        await FixAgent().suggest("cause", playbooks=[])
    instr = fake.await_args.kwargs["instruction"]
    assert "ALLOWED REMEDIATION PLAYBOOKS" in instr and "(none" in instr


def test_pipeline_strips_playbook_not_selected_for_incident() -> None:
    """Ревью #427 P1: gate не видит фактов и доверяет любой ссылке на
    существующий playbook. Ссылку, которую matcher для ЭТОГО инцидента не
    выбирал (OOM, свежий выкат, чужой алерт), pipeline снимает — и gate
    блокирует intent как playbook_missing."""
    from app.workers.pipeline import _enforce_candidate_binding
    restart = load_registry()[_RESTART]
    bound = _intent(playbook=_RESTART)
    # Кандидатов нет (preconditions не выполнены) — привязка снимается.
    stripped = _enforce_candidate_binding(bound, [], "inc-1")
    assert stripped.playbook is None
    # Выбран matcher-ом — остаётся как есть.
    assert _enforce_candidate_binding(bound, [restart], "inc-1") is bound
    # Флаг выключен (None) — поле не трогаем.
    assert _enforce_candidate_binding(bound, None, "inc-1") is bound
    assert _enforce_candidate_binding(None, [restart], "inc-1") is None


def test_stripped_intent_is_blocked_by_gate(binding_on) -> None:
    from app.workers.pipeline import _enforce_candidate_binding
    stripped = _enforce_candidate_binding(_intent(playbook=_RESTART), [], "inc-1")
    decision = evaluate_intent_gate(stripped)
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["reason"] == "playbook_missing"
