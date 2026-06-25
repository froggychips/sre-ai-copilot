"""Тесты на app.services.executor_apply.apply_intent.

Порядок проверок: record → already_applied → intent → ПОДПИСЬ (обязательна) →
signature_mismatch → ActionApproval(approved) → risk → dry_run → policy-gate →
execute.

Проверяем:
  - record не найден → incident_not_found
  - no execution_intent → no_intent
  - executor_applied уже есть → already_applied (idempotency)
  - нет expected_signature → signature_required
  - подпись не совпала → signature_mismatch
  - нет approved-записи в kg_action_approvals → not_approved
  - risk=high → risk_too_high:high
  - dry_run не прошёл → dry_run_not_ok:<status>
  - prod-namespace + LLM risk=low → policy_block (детерминированный gate)
  - happy path → execute_intent(dry_run=False, post_approval=True), executor_applied записан
  - exception в k8s_service → execute_error, не падаем
"""
from unittest.mock import MagicMock, patch

import pytest

from app.core.execution_dsl import ExecutionIntent
from app.services import executor_apply
from app.services.intent_signature import compute_signature


@pytest.fixture
def mock_session(monkeypatch):
    """Захватить SessionLocal() и вернуть mock-session с управляемым record."""
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    # apply_intent грузит record с row-lock: .filter(...).with_for_update().first()
    query.with_for_update.return_value = query
    monkeypatch.setattr(executor_apply, "SessionLocal", lambda: session)
    return session, query


def _make_record(analysis: dict) -> MagicMock:
    rec = MagicMock()
    rec.analysis = analysis
    return rec


def _valid_intent_dict() -> dict:
    return {
        "action": "restart_deployment",
        "resource_type": "deployment",
        "resource_name": "town-service",
        "namespace": "squad-1",
        "params": {},
        "risk": "low",
    }


def _sig_for(intent_dict: dict) -> str:
    return compute_signature(ExecutionIntent.model_validate(intent_dict))


class _ApprovedRow:
    status = "approved"


def _approved():
    """patch-контекст: _load_approval возвращает approved-строку."""
    return patch.object(executor_apply, "_load_approval", return_value=_ApprovedRow())


def test_apply_refuses_when_record_missing(mock_session):
    _, query = mock_session
    query.first.return_value = None

    out = executor_apply.apply_intent("missing-id", "user1", _sig_for(_valid_intent_dict()))
    assert out == {"ok": False, "reason": "incident_not_found"}


def test_apply_refuses_when_no_intent(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({"executor_result": {"status": "dry_run_ok"}})

    out = executor_apply.apply_intent("inc-1", "user1", "anysig")
    assert out == {"ok": False, "reason": "no_intent"}


def test_apply_idempotent_when_already_applied(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_applied": {
            "applied_at": "2026-05-13T12:00:00+00:00",
            "applied_by": "previous-user",
            "result": {"success": True},
        },
    })

    out = executor_apply.apply_intent("inc-4", "user1", "anysig")
    assert out == {"ok": False, "reason": "already_applied"}


def test_apply_refuses_without_signature(mock_session):
    """expected_signature обязателен — без него реальный write не запускается."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })

    out = executor_apply.apply_intent("inc-nosig", "user1")  # без подписи
    assert out == {"ok": False, "reason": "signature_required"}


def test_apply_refuses_on_signature_mismatch(mock_session):
    """Подпись из кнопки ≠ подписи intent-а в БД (TOCTOU-подмена) → отказ."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })

    out = executor_apply.apply_intent("inc-toctou", "user1", "deadbeefcafe")
    assert out == {"ok": False, "reason": "signature_mismatch"}


def test_apply_refuses_when_not_approved(mock_session):
    """Подпись ок, но нет approved-записи в kg_action_approvals → not_approved."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })

    with patch.object(executor_apply, "_load_approval", return_value=None):
        out = executor_apply.apply_intent(
            "inc-noappr", "user1", _sig_for(_valid_intent_dict())
        )
    assert out == {"ok": False, "reason": "not_approved"}


def test_apply_refuses_when_risk_high(mock_session):
    _, query = mock_session
    high_risk = {**_valid_intent_dict(), "risk": "high"}
    query.first.return_value = _make_record({
        "execution_intent": high_risk,
        "executor_result": {"status": "dry_run_ok"},
    })

    with _approved():
        out = executor_apply.apply_intent("inc-3", "user1", _sig_for(high_risk))
    assert out == {"ok": False, "reason": "risk_too_high:high"}


def test_apply_refuses_when_dry_run_failed(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "guardrail_blocked", "reason": "ns_forbidden"},
    })

    with _approved():
        out = executor_apply.apply_intent(
            "inc-2", "user1", _sig_for(_valid_intent_dict())
        )
    assert out["ok"] is False
    assert out["reason"].startswith("dry_run_not_ok:")


def test_apply_refuses_prod_intent_even_when_llm_risk_low(mock_session):
    """Headline #1: LLM-`risk=low` НЕ обходит детерминированный policy-gate.

    Подпись/approval валидны, но namespace prod-* → evaluate_intent_gate BLOCK.
    """
    _, query = mock_session
    prod_intent = {**_valid_intent_dict(), "namespace": "prod-kingdom7", "risk": "low"}
    query.first.return_value = _make_record({
        "execution_intent": prod_intent,
        "executor_result": {"status": "dry_run_ok"},
    })

    with _approved(), patch.object(executor_apply.k8s_service, "execute_intent") as mock_exec:
        out = executor_apply.apply_intent("inc-prod", "user1", _sig_for(prod_intent))

    assert out["ok"] is False
    assert out["reason"].startswith("policy_block:")
    mock_exec.assert_not_called()


def test_apply_happy_path_calls_k8s_with_post_approval(mock_session):
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    fake_result = {
        "success": True,
        "stdout": "deployment.apps/town-service restarted",
        "stderr": "",
        "command": "kubectl rollout restart deployment/town-service -n squad-1",
        "exit_code": 0,
        "dry_run": False,
    }
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent", return_value=fake_result
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-5", "discord-user-123", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    _, kwargs = mock_exec.call_args
    assert kwargs["dry_run"] is False
    assert kwargs["post_approval"] is True
    assert "executor_applied" in record.analysis
    applied = record.analysis["executor_applied"]
    assert applied["applied_by"] == "discord-user-123"
    assert applied["result"]["success"] is True
    assert "town-service restarted" in applied["result"]["stdout"]
    session.commit.assert_called_once()


def test_apply_persists_failure_result(mock_session):
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    fake_result = {
        "success": False,
        "stderr": "Error from server: forbidden",
        "exit_code": 1,
        "command": "kubectl rollout restart deployment/town-service -n squad-1",
    }
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent", return_value=fake_result
    ):
        out = executor_apply.apply_intent(
            "inc-6", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    applied = record.analysis["executor_applied"]
    assert applied["result"]["success"] is False
    assert "forbidden" in applied["result"]["stderr"]


def test_apply_catches_exception(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent",
        side_effect=RuntimeError("subprocess died"),
    ):
        out = executor_apply.apply_intent(
            "inc-7", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is False
    assert out["reason"] == "execute_error"
    assert "subprocess died" in out["error"]
