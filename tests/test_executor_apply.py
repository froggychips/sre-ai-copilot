"""Тесты на app.services.executor_apply.apply_intent.

Проверяем:
  - record не найден → reason=incident_not_found
  - no execution_intent в analysis → reason=no_intent
  - dry_run не прошёл → reason=dry_run_not_ok:<status>
  - risk=high → reason=risk_too_high:high
  - executor_applied уже есть → reason=already_applied (idempotency)
  - happy path → k8s_service.execute_intent вызван с dry_run=False, post_approval=True;
    record.analysis.executor_applied записан с timestamp + user
  - exception в k8s_service → reason=execute_error, не падаем
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import executor_apply


@pytest.fixture
def mock_session(monkeypatch):
    """Захватить SessionLocal() и вернуть mock-session с управляемым record."""
    session = MagicMock()
    query = session.query.return_value.filter.return_value
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


def test_apply_refuses_when_record_missing(mock_session):
    _, query = mock_session
    query.first.return_value = None

    out = executor_apply.apply_intent("missing-id", "user1")
    assert out == {"ok": False, "reason": "incident_not_found"}


def test_apply_refuses_when_no_intent(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({"executor_result": {"status": "dry_run_ok"}})

    out = executor_apply.apply_intent("inc-1", "user1")
    assert out == {"ok": False, "reason": "no_intent"}


def test_apply_refuses_when_dry_run_failed(mock_session):
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "guardrail_blocked", "reason": "ns_forbidden"},
    })

    out = executor_apply.apply_intent("inc-2", "user1")
    assert out["ok"] is False
    assert out["reason"].startswith("dry_run_not_ok:")


def test_apply_refuses_when_risk_high(mock_session):
    _, query = mock_session
    high_risk = {**_valid_intent_dict(), "risk": "high"}
    query.first.return_value = _make_record({
        "execution_intent": high_risk,
        "executor_result": {"status": "dry_run_ok"},
    })

    out = executor_apply.apply_intent("inc-3", "user1")
    assert out == {"ok": False, "reason": "risk_too_high:high"}


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

    out = executor_apply.apply_intent("inc-4", "user1")
    assert out == {"ok": False, "reason": "already_applied"}


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
    with patch.object(executor_apply.k8s_service, "execute_intent", return_value=fake_result) as mock_exec:
        out = executor_apply.apply_intent("inc-5", "discord-user-123")

    assert out["ok"] is True
    # k8s_service.execute_intent вызван с dry_run=False, post_approval=True
    _, kwargs = mock_exec.call_args
    assert kwargs["dry_run"] is False
    assert kwargs["post_approval"] is True
    # запись executor_applied появилась
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
    with patch.object(executor_apply.k8s_service, "execute_intent", return_value=fake_result):
        out = executor_apply.apply_intent("inc-6", "user1")

    # ok=True потому что apply-flow выполнился; внутри success=False — это видно в result.
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
    with patch.object(executor_apply.k8s_service, "execute_intent", side_effect=RuntimeError("subprocess died")):
        out = executor_apply.apply_intent("inc-7", "user1")

    assert out["ok"] is False
    assert out["reason"] == "execute_error"
    assert "subprocess died" in out["error"]
