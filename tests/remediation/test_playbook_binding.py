"""Серверная привязка intent → снимок отбора playbook-ов и verify из playbook-а.

Покрытие:
- снимок: запись на playbook, стабильный hash, digest YAML;
- gate сверяет intent со снимком: каждая причина отказа отдельно,
  шаблонный параметр принимает значение, литеральный — только своё;
- подпись intent-а включает hash снимка, прежние подписи не сдвигаются;
- `playbook_match` из вывода модели отбрасывается, ставит его только pipeline;
- verify playbook-а ужесточает исход assess(), но не ослабляет его.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.core.execution_dsl import ExecutionIntent
from app.database import IncidentRecord
from app.remediation import matcher
from app.remediation import verification as v
from app.remediation.binding import (SNAPSHOT_VERSION, BindingViolation,
                                     bind_server_params, build_match_snapshot,
                                     check_intent_binding, entry_binding,
                                     playbook_digest)
from app.remediation.executor_gate import PolicyMode, evaluate_intent_gate
from app.remediation.playbook import Playbook, load_registry
from app.services.intent_signature import compute_signature

_RESTART = "restart_crashloop_deployment"
_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


_SCALE_PARAMS = {"replicas": "{replicas}", "current_replicas": "{current_replicas}"}


def _pb(name: str = "t_scale", steps=None, **overrides) -> Playbook:
    data = {
        "schema_version": "remediation.playbook/v2",
        "name": name,
        "kind": "remediation",
        "match": {"alertnames": ["KubePodCrashLooping"]},
        "preconditions": [{"fact": "crashloop", "verdict": "found"}],
        "policy": {
            "approve": {"namespace_tier": ["dev", "squad"]},
            "block": {"any": {"namespace_tier": ["prod", "system"]}},
        },
        "plan": {"steps": steps or [
            {"action": "scale_deployment", "params": _SCALE_PARAMS},
        ]},
        "verify": ["converged", "healthy"],
    }
    data.update(overrides)
    return Playbook.model_validate(data)


def _snapshot(*pbs: Playbook, namespace: str = "squad-1") -> dict:
    return build_match_snapshot(list(pbs), facts=None, namespace=namespace,
                                alertname="KubePodCrashLooping",
                                classification=None, now=_NOW)


def _intent(action: str = "scale_deployment", namespace: str = "squad-1",
            **kw) -> ExecutionIntent:
    data = {
        "action": action, "resource_type": "deployment",
        "resource_name": "town-service", "namespace": namespace,
        "params": kw.pop("params", {"replicas": 3}), "risk": "low",
    }
    data.update(kw)
    return ExecutionIntent.model_validate(data)


def _bound(pb: Playbook, snap: dict, current: int | None = 2, **kw) -> ExecutionIntent:
    """Intent, привязанный так же, как это делает pipeline: hash записи
    снимка + серверные параметры с «живого» объекта (probe)."""
    binding = next(e["binding"] for e in snap["entries"] if e["playbook"] == pb.name)
    intent = _intent(playbook=pb.name, playbook_match=binding, **kw)
    return bind_server_params(intent, snap, lambda _i: current)


@pytest.fixture
def binding_on(monkeypatch):
    monkeypatch.setattr(settings, "REMEDIATION_PLAYBOOK_BINDING_ENABLED", True)


# ── снимок ───────────────────────────────────────────────────────────────


def test_snapshot_records_plan_verify_digest_and_stable_binding() -> None:
    pb = _pb()
    snap = _snapshot(pb)
    assert snap["version"] == SNAPSHOT_VERSION
    (entry,) = snap["entries"]
    assert entry["playbook"] == "t_scale"
    assert entry["plan"] == [{"action": "scale_deployment", "resource_type": None,
                              "params": _SCALE_PARAMS}]
    assert entry["verify"] == ["converged", "healthy"]
    assert entry["playbook_digest"] == playbook_digest(pb)
    # Факты не переданы — precondition честно помечен как невыполненный.
    assert entry["preconditions"][0]["ok"] is False
    # Hash детерминирован: тот же снимок — тот же binding.
    assert entry["binding"] == entry_binding(entry) == _snapshot(pb)["entries"][0]["binding"]


def test_playbook_digest_changes_with_yaml() -> None:
    assert playbook_digest(_pb()) != playbook_digest(_pb(verify=["converged"]))


# ── сверка intent-а со снимком ───────────────────────────────────────────


def test_bound_intent_passes_and_template_param_accepts_value() -> None:
    pb = _pb()
    snap = _snapshot(pb)
    assert check_intent_binding(_bound(pb, snap), snap, {pb.name: pb}) is pb
    assert check_intent_binding(_bound(pb, snap, params={"replicas": 5}), snap, {pb.name: pb}) is pb


def _violation(intent, snap, registry) -> str:
    with pytest.raises(BindingViolation) as exc:
        check_intent_binding(intent, snap, registry)
    return exc.value.reason


def test_violations_each_have_their_own_reason() -> None:
    pb = _pb()
    snap = _snapshot(pb)
    reg = {pb.name: pb}
    bound = _bound(pb, snap)

    assert _violation(_intent(), snap, reg) == "playbook_missing"
    assert _violation(bound, None, reg) == "match_snapshot_missing"
    assert _violation(bound, {**snap, "version": "other"}, reg) == "match_snapshot_missing"
    assert _violation(_intent(playbook=pb.name), snap, reg) == "binding_missing"
    assert _violation(_intent(playbook="other_pb", playbook_match="0" * 12),
                      snap, reg) == "playbook_not_matched"
    assert _violation(_intent(playbook=pb.name, playbook_match="0" * 12),
                      snap, reg) == "binding_mismatch"

    tampered = copy.deepcopy(snap)
    tampered["entries"][0]["plan"][0]["params"]["replicas"] = 99
    assert _violation(bound, tampered, reg) == "snapshot_tampered"

    other_ns = _snapshot(pb, namespace="squad-2")
    assert _violation(_bound(pb, other_ns), other_ns, reg) == "namespace_mismatch"

    assert _violation(bound, snap, {}) == "playbook_unknown"
    # YAML поправили после отбора (новый образ) — одобренный intent не проходит.
    assert _violation(bound, snap, {pb.name: _pb(verify=["converged"])}) \
        == "playbook_changed_since_match"


def test_intent_must_equal_a_plan_step() -> None:
    literal = _pb(name="t_literal", steps=[
        {"action": "scale_deployment",
         "params": {"replicas": 2, "current_replicas": "{current_replicas}"}},
    ])
    snap = _snapshot(literal)
    reg = {literal.name: literal}
    assert check_intent_binding(_bound(literal, snap, params={"replicas": 2}), snap, reg)
    # Литерал плана — только своё значение.
    assert _violation(_bound(literal, snap, params={"replicas": 3}), snap, reg) \
        == "intent_not_in_plan"
    # Другое действие.
    assert _violation(_bound(literal, snap, action="restart_deployment", params={}),
                      snap, reg) == "intent_not_in_plan"
    # Лишний параметр, которого план не предусматривал.
    tmpl = _pb()
    snap2 = _snapshot(tmpl)
    assert _violation(_bound(tmpl, snap2, params={"replicas": 3, "extra": 1}),
                      snap2, {tmpl.name: tmpl}) == "intent_not_in_plan"


def test_preview_only_playbook_in_snapshot_is_not_executable() -> None:
    v1 = load_registry()["cleanup_stale_failed_job"]
    snap = _snapshot(v1)
    intent = _intent(playbook=v1.name, playbook_match=snap["entries"][0]["binding"])
    assert _violation(intent, snap, {v1.name: v1}) == "playbook_not_executable"


# ── gate ─────────────────────────────────────────────────────────────────


def test_gate_uses_snapshot_not_llm_name(binding_on, monkeypatch) -> None:
    pb = _pb()
    monkeypatch.setattr(matcher, "default_registry", lambda: {pb.name: pb})
    snap = _snapshot(pb)
    assert evaluate_intent_gate(_bound(pb, snap), match_snapshot=snap).mode \
        == PolicyMode.APPROVE
    # То же имя playbook-а без снимка — отказ.
    decision = evaluate_intent_gate(_bound(pb, snap))
    assert decision.mode == PolicyMode.BLOCK
    assert decision.reasons[0]["reason"] == "match_snapshot_missing"


def test_gate_without_flag_ignores_snapshot() -> None:
    assert not settings.REMEDIATION_PLAYBOOK_BINDING_ENABLED
    intent = _intent()
    assert evaluate_intent_gate(intent) == evaluate_intent_gate(intent, match_snapshot={"x": 1})


# ── подпись и вывод модели ───────────────────────────────────────────────


def test_signature_covers_snapshot_hash_and_keeps_old_signatures() -> None:
    plain = _intent()
    with_pb = _intent(playbook="t_scale")
    bound = _intent(playbook="t_scale", playbook_match="a" * 12)
    other = _intent(playbook="t_scale", playbook_match="b" * 12)
    assert compute_signature(plain) == compute_signature(_intent())
    assert len({compute_signature(x) for x in (plain, with_pb, bound, other)}) == 4


def test_llm_cannot_set_playbook_match() -> None:
    raw = ('{"action": "scale_deployment", "resource_type": "deployment", '
           '"resource_name": "town-service", "namespace": "squad-1", '
           '"params": {"replicas": 2}, "playbook": "t_scale", '
           '"playbook_match": "not-a-hash"}')
    intent = ExecutionIntent.from_llm_response(raw)
    assert intent is not None
    assert intent.playbook == "t_scale" and intent.playbook_match is None


def test_pipeline_sets_binding_from_snapshot_and_strips_foreign_value() -> None:
    from app.workers.pipeline import _enforce_candidate_binding
    pb = _pb()
    snap = _snapshot(pb)
    out = _enforce_candidate_binding(_intent(playbook=pb.name), [pb], "inc-1", snap)
    assert out.playbook_match == snap["entries"][0]["binding"]
    # Значение, которое не ставил сервер, затирается — и при выключенном флаге.
    forged = _intent(playbook=pb.name, playbook_match="c" * 12)
    assert _enforce_candidate_binding(forged, None, "inc-1").playbook_match is None
    # Playbook не выбран matcher-ом — снимаются и имя, и hash.
    with patch("app.workers.pipeline.audit_service.log_event"):
        stripped = _enforce_candidate_binding(forged, [], "inc-1", snap)
    assert stripped.playbook is None and stripped.playbook_match is None


# ── verify из playbook-а ─────────────────────────────────────────────────


def _result(outcome: str, **checks) -> dict:
    return {"outcome": outcome, "checks": checks, "reasons": []}


def _verify(*names: str) -> dict:
    return {"playbook": "t_scale", "source": "match_snapshot", "names": list(names)}


def test_playbook_verify_without_list_changes_nothing() -> None:
    res = _result("verified", converged=True)
    assert v.apply_playbook_verify(res, None, attempt=1, max_attempts=2) is res


def test_playbook_verify_gap_is_not_success() -> None:
    res = _result("verified", same_identity=None, converged=True)
    mid = v.apply_playbook_verify(res, _verify("same_identity", "converged"),
                                  attempt=1, max_attempts=2)
    assert mid["outcome"] == "pending"
    last = v.apply_playbook_verify(res, _verify("same_identity", "converged"),
                                   attempt=2, max_attempts=2)
    assert last["outcome"] == "unknown"
    assert last["playbook_verify"]["checks"] == {"same_identity": None, "converged": True}


def test_playbook_verify_false_fails_on_last_attempt() -> None:
    res_false = _result("pending", converged=False)
    assert v.apply_playbook_verify(res_false, _verify("converged"),
                                   attempt=1, max_attempts=2)["outcome"] == "pending"
    assert v.apply_playbook_verify(res_false, _verify("converged"),
                                   attempt=2, max_attempts=2)["outcome"] == "failed"


def test_playbook_verify_ignores_checks_it_does_not_list() -> None:
    # alert_resolved не проверен, но playbook его не требует — успех остаётся.
    res = _result("verified", alert_resolved=None, healthy=True)
    assert v.apply_playbook_verify(res, _verify("healthy"),
                                   attempt=1, max_attempts=2)["outcome"] == "verified"


def test_playbook_verify_never_loosens_assess() -> None:
    for outcome in ("failed", "unknown"):
        res = _result(outcome, converged=True, healthy=True)
        out = v.apply_playbook_verify(res, _verify("converged", "healthy"),
                                      attempt=2, max_attempts=2)
        assert out["outcome"] == outcome


def test_verify_names_only_for_server_bound_intent(monkeypatch) -> None:
    pb = _pb()
    snap = _snapshot(pb)
    bound = _bound(pb, snap)
    got = v.playbook_verify_names(bound, {"playbook_match": snap})
    assert got == {"playbook": "t_scale", "source": "match_snapshot",
                   "names": ["converged", "healthy"]}
    # Снимок потерян — реестр НЕ подставляется: его редакция могла выкинуть
    # обязательную проверку. Исход — binding_lost, не verified.
    monkeypatch.setattr(matcher, "default_registry", lambda: {pb.name: pb})
    lost = v.playbook_verify_names(bound, {})
    assert lost["source"] == "binding_lost" and lost["binding_lost"] is True
    # Имя без серверного hash-а (модель вписала при выключенном флаге) — не привязка.
    assert v.playbook_verify_names(_intent(playbook=pb.name), {"playbook_match": snap}) is None


def test_verify_remediation_applies_playbook_checks(monkeypatch) -> None:
    """Сквозной путь: restart-playbook требует same_identity, а uid до
    действия неизвестен — без playbook-а это verified, с ним — не успех."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from tests.remediation.test_verification import _deploy_json, _runner, _seed_incident

    monkeypatch.setattr(v.settings, "REMEDIATION_VERIFY_DELAYS_SEC", "300,900", raising=False)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    restart = load_registry()[_RESTART]
    snap = _snapshot(restart)
    db = Session()
    rec = _seed_incident(db, {"generation": 5})
    analysis = dict(rec.analysis)
    analysis["execution_intent"] = {
        **analysis["execution_intent"],
        "playbook": _RESTART,
        "playbook_match": snap["entries"][0]["binding"],
    }
    analysis["playbook_match"] = snap
    rec.analysis = analysis
    db.commit()
    db.close()
    with patch("app.services.audit_logger.audit_service.log_event"):
        out = v.verify_remediation("inc-1", 2, db_factory=Session,
                                   runner=_runner(_deploy_json(generation=6, observed=6)))
    assert out["outcome"] == "unknown"
    ver = Session().query(IncidentRecord).filter_by(incident_id="inc-1").one() \
        .analysis["executor_verification"]
    assert ver["playbook_verify"]["checks"]["same_identity"] is None
    engine.dispose()


# ── scale-playbook и classification ──────────────────────────────────────

_SCALE = "scale_out_cpu_throttled_deployment"
_HEALTHY = {"resource_pressure": "found", "crashloop": "absent",
            "oom_killed": "absent", "recent_deploy": "absent"}


def _store(**verdicts):
    from app.diagnostics.facts import Fact, FactStore
    store = FactStore()
    for kind, verdict in verdicts.items():
        if verdict == "unknown":
            store.add(Fact.unknown(kind, "source down"))
        else:
            store.add(Fact(kind=kind, observed=verdict == "found", confidence=0.9,
                           verdict=verdict))
    return store


def _names(reg, alertname, facts):
    return [pb.name for pb in matcher.match_playbooks(reg, alertname=alertname, facts=facts)]


@pytest.mark.parametrize("kind, verdict", [
    ("recent_deploy", "found"), ("oom_killed", "found"),
    ("crashloop", "found"), ("recent_deploy", "unknown"),
])
def test_scale_playbook_needs_healthy_process(kind, verdict) -> None:
    reg = load_registry()
    assert _names(reg, "CPUThrottlingHigh", _store(**_HEALTHY)) == [_SCALE]
    assert _names(reg, "CPUThrottlingHigh", _store(**{**_HEALTHY, kind: verdict})) == []


def test_scale_playbook_only_for_its_alert() -> None:
    assert _SCALE not in _names(load_registry(), "KubePodCrashLooping", _store(**_HEALTHY))


def test_scale_playbook_gate_approves_squad_blocks_prod_and_data_plane(binding_on) -> None:
    scale = load_registry()[_SCALE]
    for ns, name, want in (("squad-1", "town-service", PolicyMode.APPROVE),
                           ("prod-k1", "town-service", PolicyMode.BLOCK),
                           ("squad-1", "town-postgres", PolicyMode.BLOCK)):
        snap = _snapshot(scale, namespace=ns)
        intent = _bound(scale, snap, namespace=ns, resource_name=name)
        assert evaluate_intent_gate(intent, match_snapshot=snap).mode == want, (ns, name)


def test_classify_alert_is_fail_closed() -> None:
    # Лейблов для классов с сигналами нет — классификации нет, а не угаданная.
    assert matcher.classify_alert({"alertname": "CPUThrottlingHigh", "namespace": "squad-1"}) is None
    assert matcher.classify_alert(None) is None


def test_classification_reaches_matcher() -> None:
    pb = _pb(name="t_by_class", match={"classification": "memory_pressure"})
    reg = {pb.name: pb}
    facts = _store(crashloop="found")
    assert matcher.match_playbooks(reg, classification="memory_pressure", facts=facts) == [pb]
    assert matcher.match_playbooks(reg, classification=None, facts=facts) == []
