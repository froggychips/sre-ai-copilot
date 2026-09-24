"""Контракт двух исполнимых playbook-ов: restart_crashloop_deployment и
scale_out_cpu_throttled_deployment.

Каждая ветка отказа — отдельный тест, по слоям в том порядке, в каком их
проходит действие (см. docs/REMEDIATION_PLAYBOOK_CONTRACT.md):

  preconditions → policy → привязка к снимку → gate → пере-dry-run
  → попытка (claim) → verify

Реальные YAML из реестра, а не фикстуры: контракт — это то, что уедет в
образ. Флаг REMEDIATION_PLAYBOOK_BINDING_ENABLED включается только в
тестах gate, где он и читается.
"""
from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from app.config import settings
from app.core.execution_dsl import DSLTranslator, ExecutionIntent
from app.diagnostics.facts import Fact, FactStore
from app.remediation import matcher
from app.remediation import verification as v
from app.remediation.binding import (BindingViolation, bind_server_params,
                                     bound_entry_for, build_match_snapshot,
                                     check_intent_binding)
from app.remediation.executor_gate import PolicyMode, evaluate_intent_gate
from app.remediation.playbook import Playbook, load_registry
from app.services import executor_apply
from app.services.intent_signature import compute_signature
from tests.test_executor_apply import (_approved, _fake_exec,  # noqa: F401
                                       _make_record, mock_session)

RESTART = "restart_crashloop_deployment"
SCALE = "scale_out_cpu_throttled_deployment"

_REG = load_registry()

# Базовые вердикты, при которых playbook обязан совпасть.
_RESTART_OK = {"crashloop": "found", "oom_killed": "absent", "recent_deploy": "absent",
               "process_crash": ("found", {"exit_code": 1})}
_SCALE_OK = {"resource_pressure": "found", "crashloop": "absent",
             "oom_killed": "absent", "recent_deploy": "absent"}
_CASES = {
    RESTART: ("KubePodCrashLooping", _RESTART_OK, "restart_deployment", {}),
    SCALE: ("CPUThrottlingHigh", _SCALE_OK, "scale_deployment", {"replicas": 4}),
}


def _fact(kind: str, verdict: str, evidence: dict | None = None) -> Fact:
    if verdict == "unknown":
        return Fact.unknown(kind, "source down")
    return Fact(kind=kind, observed=verdict == "found", confidence=0.9,
                verdict=verdict, evidence=evidence or {})


def _store(verdicts: dict) -> FactStore:
    store = FactStore()
    for kind, spec in verdicts.items():
        specs = spec if isinstance(spec, list) else [spec]
        for one in specs:
            verdict, evidence = one if isinstance(one, tuple) else (one, None)
            if verdict == "missing":
                continue
            store.add(_fact(kind, verdict, evidence))
    return store


def _matched(name: str, verdicts: dict) -> bool:
    alertname = _CASES[name][0]
    return name in [pb.name for pb in matcher.match_playbooks(
        _REG, alertname=alertname, facts=_store(verdicts))]


def _snapshot(name: str, namespace: str = "squad-1") -> dict:
    return build_match_snapshot([_REG[name]], facts=_store(_CASES[name][1]),
                                namespace=namespace, alertname=_CASES[name][0],
                                classification=None)


def _intent(name: str, namespace: str = "squad-1", resource: str = "town-service",
            **kw) -> ExecutionIntent:
    _, _, action, params = _CASES[name]
    data = {"action": action, "resource_type": "deployment", "resource_name": resource,
            "namespace": namespace, "params": dict(params), "risk": "low"}
    data.update(kw)
    return ExecutionIntent.model_validate(data)


def _bind(name: str, snap: dict, current: int | None = 3, **kw) -> ExecutionIntent:
    """Привязка так же, как в pipeline: hash записи + серверные параметры."""
    binding = snap["entries"][0]["binding"]
    intent = _intent(name, playbook=name, playbook_match=binding, **kw)
    return bind_server_params(intent, snap, lambda _i: current)


def _reason(intent, snap, registry=None) -> str:
    with pytest.raises(BindingViolation) as exc:
        check_intent_binding(intent, snap, registry or _REG)
    return exc.value.reason


@pytest.fixture
def binding_on(monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", True)
    monkeypatch.setattr(matcher, "default_registry", lambda: _REG)


# ── 1. preconditions ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_baseline_facts_match(name) -> None:
    assert _matched(name, _CASES[name][1])


def _breakers(name: str):
    """Для каждого precondition — все вердикты, которые обязаны его сломать.

    found-условие: ABSENT, UNKNOWN, факта нет.
    absent-условие: FOUND, UNKNOWN, факта нет и оба конфликта — «один
    источник нашёл, другой нет» и «один не нашёл, другой упал».
    """
    for pre in _REG[name].preconditions or ():
        yield pre.fact, "unknown"
        yield pre.fact, "missing"
        if pre.verdict == "found":
            yield pre.fact, "absent"
        else:
            yield pre.fact, ("found", {"exit_code": 1})
            yield pre.fact, [("found", {"exit_code": 1}), "absent"]
            yield pre.fact, ["absent", "unknown"]


@pytest.mark.parametrize("name, fact, verdict", [
    (name, fact, verdict) for name in (RESTART, SCALE) for fact, verdict in _breakers(name)
])
def test_each_precondition_breaks_match(name, fact, verdict) -> None:
    facts = {**_CASES[name][1], fact: verdict}
    assert not _matched(name, facts), (fact, verdict)


def test_found_condition_survives_a_neighbouring_unknown() -> None:
    # Найденное найдено: соседний упавший источник не отменяет FOUND.
    facts = {**_RESTART_OK, "crashloop": ["found", "unknown"]}
    assert _matched(RESTART, facts)


@pytest.mark.parametrize("exit_code", [132, 134, 135, 136, 138, 139])
def test_restart_refuses_signal_crash(exit_code) -> None:
    facts = {**_RESTART_OK, "process_crash": ("found", {"exit_code": exit_code})}
    assert not _matched(RESTART, facts)


def test_restart_refuses_crash_without_exit_code_evidence() -> None:
    assert not _matched(RESTART, {**_RESTART_OK, "process_crash": ("found", {})})


@pytest.mark.parametrize("name, alertname", [
    (RESTART, "CPUThrottlingHigh"), (SCALE, "KubePodCrashLooping"), (SCALE, None),
])
def test_other_alert_does_not_match(name, alertname) -> None:
    got = matcher.match_playbooks(_REG, alertname=alertname, facts=_store(_CASES[name][1]))
    assert name not in [pb.name for pb in got]


# ── 2. policy (gate при включённой привязке) ─────────────────────────────


@pytest.mark.parametrize("name", [RESTART, SCALE])
@pytest.mark.parametrize("namespace, resource, want", [
    ("squad-1", "town-service", PolicyMode.APPROVE),
    ("dev-7", "town-service", PolicyMode.APPROVE),
    ("prod-k1", "town-service", PolicyMode.BLOCK),
    ("preprod-k1", "town-service", PolicyMode.BLOCK),
    ("monitoring", "town-service", PolicyMode.BLOCK),     # system
    ("sandbox-x", "town-service", PolicyMode.BLOCK),      # неизвестный tier = system
    ("squad-1", "town-postgres", PolicyMode.BLOCK),       # data-plane по имени
    ("squad-1", "chat-redis", PolicyMode.BLOCK),
])
def test_policy_tiers(binding_on, name, namespace, resource, want) -> None:
    snap = _snapshot(name, namespace=namespace)
    intent = _bind(name, snap, namespace=namespace, resource=resource)
    assert evaluate_intent_gate(intent, match_snapshot=snap).mode == want


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_never_auto(binding_on, name) -> None:
    # У обоих playbook-ов нет policy.auto: лучший исход — одобрение человеком.
    assert _REG[name].policy.auto is None


# ── 3. привязка к серверному снимку ──────────────────────────────────────


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_bound_intent_passes(name) -> None:
    snap = _snapshot(name)
    assert check_intent_binding(_bind(name, snap), snap, _REG).name == name


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_binding_refusals(name) -> None:
    snap = _snapshot(name)
    bound = _bind(name, snap)
    # нет снимка
    assert _reason(bound, None) == "match_snapshot_missing"
    # нет hash-а в intent-е
    assert _reason(_intent(name, playbook=name), snap) == "binding_missing"
    # чужой снимок: другой namespace (другой инцидент) — hash не сойдётся
    foreign = _snapshot(name, namespace="squad-2")
    assert _reason(bound, foreign) == "binding_mismatch"
    # снимок другого playbook-а
    other = SCALE if name == RESTART else RESTART
    assert _reason(bound, _snapshot(other)) == "playbook_not_matched"
    # план в снимке поправили задним числом
    tampered = copy.deepcopy(snap)
    tampered["entries"][0]["verify"] = []
    assert _reason(bound, tampered) == "snapshot_tampered"
    # YAML playbook-а поменялся после отбора (новый образ)
    changed = {name: Playbook.model_validate(
        {**_REG[name].model_dump(mode="json", exclude_none=True), "verify": ["converged"]})}
    assert _reason(bound, snap, changed) == "playbook_changed_since_match"
    # intent — не шаг плана (другое действие)
    wrong = bound.model_copy(update={
        "action": _intent(other).action, "params": dict(_CASES[other][3])})
    assert _reason(wrong, snap) in ("intent_not_in_plan", "server_param_missing")


def test_stale_intent_is_refused_on_apply(mock_session, monkeypatch) -> None:  # noqa: F811
    """«Протухший» план: возраст intent-а > EXECUTOR_INTENT_MAX_AGE_SECONDS —
    отказ до gate и до kubectl (общий путь executor_apply для обоих)."""
    from datetime import datetime, timedelta, timezone
    session, query = mock_session
    snap = _snapshot(RESTART)
    intent = _bind(RESTART, snap)
    record = _make_record({"execution_intent": intent.model_dump(mode="json"),
                           "executor_result": {"status": "dry_run_ok"},
                           "playbook_match": snap})
    record.created_at = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None)
    query.first.return_value = record
    with _approved(), patch.object(executor_apply.k8s_service, "execute_intent") as ex:
        out = executor_apply.apply_intent("inc-s", "u1", compute_signature(intent))
    assert out["ok"] is False and out["reason"].startswith("intent_stale")
    ex.assert_not_called()


# ── 4. scale: current_replicas и конкурентный скейл ──────────────────────


def test_scale_server_param_lands_in_snapshot_and_argv() -> None:
    snap = _snapshot(SCALE)
    before = snap["entries"][0]["binding"]
    intent = _bind(SCALE, snap, current=3)
    entry = snap["entries"][0]
    assert entry["server_params"] == {"resource_name": "town-service", "current_replicas": 3}
    # hash пересчитан и intent несёт уже его — подпись покрывает снятое значение
    assert entry["binding"] != before and intent.playbook_match == entry["binding"]
    assert "--current-replicas=3" in DSLTranslator.to_argv(intent)


def test_scale_model_cannot_choose_current_replicas() -> None:
    raw = ('{"action": "scale_deployment", "resource_type": "deployment", '
           '"resource_name": "town-service", "namespace": "squad-1", '
           '"params": {"replicas": 4, "current_replicas": 99}}')
    intent = ExecutionIntent.from_llm_response(raw)
    assert intent is not None and "current_replicas" not in intent.params
    snap = _snapshot(SCALE)
    forged = intent.model_copy(update={
        "playbook": SCALE, "playbook_match": snap["entries"][0]["binding"],
        "params": {"replicas": 4, "current_replicas": 99}})
    # сервер снимает своё значение поверх вписанного
    assert bind_server_params(forged, snap, lambda _i: 2).params["current_replicas"] == 2


def test_scale_without_live_replicas_is_refused() -> None:
    snap = _snapshot(SCALE)
    intent = _bind(SCALE, snap, current=None)
    assert "current_replicas" not in intent.params
    assert _reason(intent, snap) == "server_param_missing"


def test_scale_probe_exception_is_refused_not_raised() -> None:
    snap = _snapshot(SCALE)
    binding = snap["entries"][0]["binding"]
    intent = _intent(SCALE, playbook=SCALE, playbook_match=binding)

    def boom(_i):
        raise RuntimeError("kubectl down")
    assert _reason(bind_server_params(intent, snap, boom), snap) == "server_param_missing"


def test_scale_current_replicas_must_equal_snapshot() -> None:
    snap = _snapshot(SCALE)
    intent = _bind(SCALE, snap, current=3)
    tweaked = intent.model_copy(update={"params": {**intent.params, "current_replicas": 5}})
    assert _reason(tweaked, snap) == "server_param_mismatch"
    other_target = intent.model_copy(update={"resource_name": "other-service"})
    assert _reason(other_target, snap) == "server_param_mismatch"


def test_scale_playbook_schema_requires_current_replicas_template() -> None:
    data = _REG[SCALE].model_dump(mode="json", exclude_none=True)
    data["plan"]["steps"][1]["params"] = {"replicas": "{replicas}"}
    with pytest.raises(ValueError, match="PRECONDITION"):
        Playbook.model_validate(data)
    data["plan"]["steps"][1]["params"] = {"replicas": "{replicas}", "current_replicas": 3}
    with pytest.raises(ValueError, match="PRECONDITION"):
        Playbook.model_validate(data)
    # Серверный параметр литералом запрещён у любого действия.
    restart = _REG[RESTART].model_dump(mode="json", exclude_none=True)
    restart["plan"]["steps"][1]["params"] = {"current_replicas": 3}
    with pytest.raises(ValueError, match="серверный параметр"):
        Playbook.model_validate(restart)


def test_concurrent_scale_fails_pre_write_dry_run(mock_session, monkeypatch) -> None:  # noqa: F811
    """HPA отскейлил между одобрением и кликом: пере-dry-run с
    --current-replicas падает у apiserver, write не выполняется."""
    session, query = mock_session
    snap = _snapshot(SCALE)
    intent = _bind(SCALE, snap, current=3)
    record = _make_record({"execution_intent": intent.model_dump(mode="json"),
                           "executor_result": {"status": "dry_run_ok"},
                           "playbook_match": snap})
    query.first.return_value = record
    seen: list = []

    def fake(i, dry_run=True, post_approval=False, **kw):
        seen.append((dry_run, DSLTranslator.to_argv(i)))
        return {"success": False, "exit_code": 1,
                "stderr": "Expected replicas to be 3, was 5", "command": "kubectl scale"}
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", True)
    monkeypatch.setattr(matcher, "default_registry", lambda: _REG)
    with _approved(), patch.object(executor_apply.k8s_service, "execute_intent",
                                   side_effect=fake):
        out = executor_apply.apply_intent("inc-c", "u1", compute_signature(intent))
    assert out["ok"] is False and out["reason"].startswith("dry_run_recheck_failed")
    assert seen == [(True, DSLTranslator.to_argv(intent))]
    assert "--current-replicas=3" in seen[0][1]


# ── 5. попытка: снимок едет вместе с ней, повтор отсечён ─────────────────


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_claim_carries_bound_entry(mock_session, monkeypatch, name) -> None:  # noqa: F811
    session, query = mock_session
    snap = _snapshot(name)
    intent = _bind(name, snap)
    record = _make_record({"execution_intent": intent.model_dump(mode="json"),
                           "executor_result": {"status": "dry_run_ok"},
                           "playbook_match": snap})
    query.first.return_value = record
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", True)
    monkeypatch.setattr(matcher, "default_registry", lambda: _REG)
    from app.remediation import attempts as attempts_store
    claims: list = []
    real = attempts_store.new_claim

    def spy(*a, **kw):
        row = real(*a, **kw)
        claims.append(row)
        return row
    with _approved(), patch.object(attempts_store, "new_claim", side_effect=spy), \
            patch.object(executor_apply.k8s_service, "execute_intent",
                         side_effect=_fake_exec()):
        out = executor_apply.apply_intent("inc-a", "u1", compute_signature(intent))
    assert out["ok"] is True, out
    (row,) = claims
    assert row.intent[attempts_store.BOUND_ENTRY_KEY] == bound_entry_for(intent, snap)


def test_second_apply_of_same_incident_is_refused(mock_session) -> None:  # noqa: F811
    """Идемпотентность: executor_applied уже есть — второй write не идёт."""
    session, query = mock_session
    snap = _snapshot(RESTART)
    intent = _bind(RESTART, snap)
    record = _make_record({"execution_intent": intent.model_dump(mode="json"),
                           "executor_result": {"status": "dry_run_ok"},
                           "executor_applied": {"applied_at": "2026-09-24T10:00:00+00:00"},
                           "playbook_match": snap})
    query.first.return_value = record
    with _approved(), patch.object(executor_apply.k8s_service, "execute_intent") as ex:
        out = executor_apply.apply_intent("inc-i", "u1", compute_signature(intent))
    assert out["ok"] is False and out["reason"] == "already_applied"
    ex.assert_not_called()


# ── 6. verify: по одобренному снимку, а не по текущему ───────────────────


def _res(outcome="verified", **checks):
    base = {"same_identity": True, "action_took_effect": True, "converged": True,
            "healthy": True, "new_crash_events": 0}
    base.update(checks)
    return {"outcome": outcome, "checks": base, "reasons": []}


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_verify_outcomes(name) -> None:
    snap = _snapshot(name)
    intent = _bind(name, snap)
    names = v.playbook_verify_names(intent, {"playbook_match": snap})
    assert names["source"] == "match_snapshot" and names["names"] == list(_REG[name].verify)
    ok = v.apply_playbook_verify(_res(), names, attempt=2, max_attempts=2)
    assert ok["outcome"] == "verified"
    bad = v.apply_playbook_verify(_res(converged=False), names, attempt=2, max_attempts=2)
    assert bad["outcome"] == "failed"
    gap = v.apply_playbook_verify(_res(same_identity=None), names, attempt=2, max_attempts=2)
    assert gap["outcome"] == "unknown"
    mid = v.apply_playbook_verify(_res(same_identity=None), names, attempt=1, max_attempts=2)
    assert mid["outcome"] == "pending"


@pytest.mark.parametrize("name", [RESTART, SCALE])
def test_refire_cannot_swap_verify_list(name) -> None:
    snap = _snapshot(name)
    intent = _bind(name, snap)
    bound = bound_entry_for(intent, snap)
    # re-fire: новый отбор с урезанным verify заменил analysis.playbook_match
    refired = copy.deepcopy(snap)
    refired["entries"][0]["verify"] = ["converged"]
    from app.remediation.binding import entry_binding
    refired["entries"][0]["binding"] = entry_binding(refired["entries"][0])
    got = v.playbook_verify_names(intent, {"playbook_match": refired}, bound)
    assert got["source"] == "attempt" and got["names"] == list(_REG[name].verify)
    # Попытка без сохранённой записи (до этого поля) + чужой снимок — binding_lost
    lost = v.playbook_verify_names(intent, {"playbook_match": refired}, None)
    assert lost["binding_lost"] is True
    out = v.apply_playbook_verify(_res(), lost, attempt=2, max_attempts=2)
    assert out["outcome"] == "unknown"
    assert v.apply_playbook_verify(_res(), lost, attempt=1, max_attempts=2)["outcome"] \
        == "pending"


def test_tampered_bound_entry_is_not_trusted() -> None:
    snap = _snapshot(RESTART)
    intent = _bind(RESTART, snap)
    bound = copy.deepcopy(bound_entry_for(intent, snap))
    bound["verify"] = []
    got = v.playbook_verify_names(intent, {}, bound)
    assert got["binding_lost"] is True


def test_verify_never_loosens_assess() -> None:
    snap = _snapshot(SCALE)
    names = v.playbook_verify_names(_bind(SCALE, snap), {"playbook_match": snap})
    for outcome in ("failed", "unknown"):
        assert v.apply_playbook_verify(_res(outcome), names, attempt=2,
                                       max_attempts=2)["outcome"] == outcome


def test_pipeline_probe_reports_unknown_as_none(monkeypatch) -> None:
    """Снимок цели не снялся — probe отдаёт None, и scale откажет
    `server_param_missing`, а не пойдёт без precondition."""
    from app.remediation.verification import TargetSnapshot
    from app.workers import pipeline
    intent = _intent(SCALE)
    monkeypatch.setattr("app.remediation.verification.snapshot_target",
                        lambda i, **kw: TargetSnapshot.unavailable("down", i))
    assert pipeline._probe_current_replicas(intent) is None
    live = TargetSnapshot.unavailable("x", intent)
    live.unknown, live.replicas_desired = False, 4
    monkeypatch.setattr("app.remediation.verification.snapshot_target",
                        lambda i, **kw: live)
    assert pipeline._probe_current_replicas(intent) == 4


def test_scale_live_replicas_above_intent_limit_still_approvable() -> None:
    """Живой Deployment на 101 реплике: current_replicas — состояние кластера,
    лимит 100 у replicas к нему не относится; даунскейл до 4 валиден."""
    snap = _snapshot(SCALE)
    intent = _bind(SCALE, snap, current=101)
    again = ExecutionIntent.model_validate(intent.model_dump(mode="json"))
    assert again.params == {"replicas": 4, "current_replicas": 101}
    assert check_intent_binding(again, snap, _REG).name == SCALE
