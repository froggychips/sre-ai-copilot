"""Тесты на app.services.executor_apply.apply_intent.

Порядок проверок: record → already_applied → in_flight-клейм → intent →
ПОДПИСЬ (обязательна) → signature_mismatch → ActionApproval(approved) →
свежесть approve → cluster_state_unknown → namespace-binding intent ↔ инцидент
→ risk → dry_run → свежесть intent-а → policy-gate → пере-dry-run →
claim-commit → execute → финализация.

Проверяем:
  - record не найден → incident_not_found
  - no execution_intent → no_intent
  - executor_applied уже есть → already_applied (idempotency)
  - свежий executor_in_flight → apply_in_flight (двухфазный claim)
  - протухший executor_in_flight → claim снят, но БЕЗ второго write:
    инцидент помечен cluster_state_unknown → manual (M2)
  - нет expected_signature → signature_required
  - подпись не совпала → signature_mismatch
  - нет approved-записи в kg_action_approvals → not_approved
  - approve старше окна / без decided_at → approval_stale
  - namespace intent-а ≠ namespace инцидента → namespace_mismatch
  - у инцидента нет namespace → namespace_unbound (fail-closed)
  - risk=high → risk_too_high:high
  - dry_run не прошёл → dry_run_not_ok:<status>
  - intent старше EXECUTOR_INTENT_MAX_AGE_SECONDS → intent_stale, без kubectl (M1)
  - возраст intent-а неопределим → intent_age_unknown (fail-closed)
  - пере-dry-run перед write упал → dry_run_recheck_failed, реального write нет
  - prod-namespace + LLM risk=low → policy_block (детерминированный gate)
  - happy path → execute_intent(dry_run=False, post_approval=True), executor_applied записан
  - claim коммитится ДО реального execute_intent (crash-window)
  - exception в k8s_service → execute_error, claim остаётся (fail-closed)
"""
from datetime import datetime, timedelta, timezone
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
    # Верификация (remediation/verification.py) ходит в kubectl и в брокер.
    # В юнит-тестах ни того ни другого: снимок «недоступен», планирование
    # выключено. Поведение самой верификации — в tests/remediation/.
    from app.remediation.verification import TargetSnapshot
    monkeypatch.setattr(
        executor_apply, "snapshot_target",
        lambda intent, **kw: TargetSnapshot.unavailable("test", intent),
    )
    monkeypatch.setattr(executor_apply, "expected_identity", lambda db, incident_id: None)
    monkeypatch.setattr(
        executor_apply, "schedule_verification",
        lambda incident_id, **kw: {"scheduled": False, "reason": "test"},
    )
    return session, query


def _make_record(analysis: dict, data: dict | None = None) -> MagicMock:
    rec = MagicMock()
    rec.analysis = analysis
    # record.data = сохранённый alert-payload; namespace нужен для
    # server-side binding intent ↔ инцидент.
    rec.data = {"namespace": "squad-1"} if data is None else data
    # created_at — последний по грубости якорь возраста intent-а (naive UTC,
    # как DateTime-колонка Postgres). Без него apply fail-closed отказал бы
    # с intent_age_unknown: возраст плана неизвестен.
    rec.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return rec


def _fake_exec(write_result=None, dry_run_result=None, write_exc=None):
    """Заглушка k8s_service.execute_intent, различающая пере-dry-run и write.

    apply_intent теперь дважды дёргает executor: сначала
    `--dry-run=server` по текущему состоянию кластера, затем реальный write.
    Тестам нужно управлять исходами по отдельности.
    """
    def _fake(intent, dry_run=True, post_approval=False, **kw):
        if dry_run:
            return dry_run_result or {
                "success": True,
                "command": "kubectl rollout restart deployment/town-service "
                           "-n squad-1 --dry-run=server",
                "exit_code": 0,
            }
        if write_exc is not None:
            raise write_exc
        return write_result or {"success": True, "command": "kubectl ..."}

    return _fake


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

    def __init__(self, age_seconds: int = 0):
        self.decided_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=age_seconds)
        )


def _approved(age_seconds: int = 0):
    """patch-контекст: _load_approval возвращает approved-строку нужного возраста."""
    return patch.object(
        executor_apply, "_load_approval", return_value=_ApprovedRow(age_seconds)
    )


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
    query.first.return_value = _make_record(
        {
            "execution_intent": prod_intent,
            "executor_result": {"status": "dry_run_ok"},
        },
        data={"namespace": "prod-kingdom7"},  # binding проходит → до policy-gate
    )

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
        executor_apply.k8s_service, "execute_intent",
        side_effect=_fake_exec(write_result=fake_result),
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-5", "discord-user-123", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    # Два вызова: пере-dry-run по текущему кластеру, затем реальный write.
    assert mock_exec.call_count == 2
    assert mock_exec.call_args_list[0].kwargs["dry_run"] is True
    _, kwargs = mock_exec.call_args
    assert kwargs["dry_run"] is False
    assert kwargs["post_approval"] is True
    assert "executor_applied" in record.analysis
    applied = record.analysis["executor_applied"]
    assert applied["applied_by"] == "discord-user-123"
    assert applied["result"]["success"] is True
    assert "town-service restarted" in applied["result"]["stdout"]
    # Двухфазность: commit claim-а + commit финализации.
    assert session.commit.call_count == 2
    # Claim снят после успешной финализации.
    assert "executor_in_flight" not in record.analysis


def test_apply_persists_failure_result(mock_session):
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    # Пере-dry-run проходит (кластер валидирует команду), а реальный write
    # падает по RBAC — результат должен быть записан, а не потерян.
    fake_result = {
        "success": False,
        "stderr": "Error from server: forbidden",
        "exit_code": 1,
        "command": "kubectl rollout restart deployment/town-service -n squad-1",
    }
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent",
        side_effect=_fake_exec(write_result=fake_result),
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


# ---------------------------------------------------------------------------
# Freshness-binding approve (#2)
# ---------------------------------------------------------------------------

def test_apply_refuses_stale_approval(mock_session):
    """Approve старше EXECUTOR_APPROVAL_MAX_AGE_SECONDS → approval_stale."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })

    with _approved(age_seconds=3600 * 5), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-stale", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "approval_stale"}
    mock_exec.assert_not_called()


def test_apply_refuses_approval_without_decided_at(mock_session):
    """Недатированный approve (decided_at=None) → approval_stale (fail-closed)."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })

    row = _ApprovedRow()
    row.decided_at = None
    with patch.object(executor_apply, "_load_approval", return_value=row), \
         patch.object(executor_apply.k8s_service, "execute_intent") as mock_exec:
        out = executor_apply.apply_intent(
            "inc-nodate", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "approval_stale"}
    mock_exec.assert_not_called()


def test_apply_accepts_fresh_approval(mock_session):
    """Approve в пределах окна проходит freshness-проверку."""
    _, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    with _approved(age_seconds=60), patch.object(
        executor_apply.k8s_service, "execute_intent",
        return_value={"success": True, "command": "kubectl ..."},
    ):
        out = executor_apply.apply_intent(
            "inc-fresh", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Двухфазный claim (#3): crash-window между kubectl и записью результата
# ---------------------------------------------------------------------------

def test_apply_commits_claim_before_execute(mock_session):
    """executor_in_flight коммитится ДО execute_intent — окно краша закрыто."""
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    order: list = []
    session.commit.side_effect = lambda: order.append("commit")

    def fake_execute(intent, dry_run=True, post_approval=False, **kw):
        order.append("dry_run" if dry_run else "execute")
        return {"success": True, "command": "kubectl ..."}

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent", side_effect=fake_execute
    ):
        out = executor_apply.apply_intent(
            "inc-order", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    # Пере-dry-run — ДО claim-а (read-only проверка не должна оставлять
    # in-flight маркер), claim — строго раньше реального write, финализация после.
    assert order == ["dry_run", "commit", "execute", "commit"]


def test_apply_crash_after_claim_leaves_claim_and_blocks_retry(mock_session):
    """Краш в kubectl-окне: claim остаётся, повторный apply → apply_in_flight."""
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    # Пере-dry-run прошёл, реальный write повис → claim остаётся.
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent",
        side_effect=_fake_exec(write_exc=TimeoutError("kubectl hang")),
    ):
        out1 = executor_apply.apply_intent(
            "inc-crash", "user1", _sig_for(_valid_intent_dict())
        )

    assert out1["ok"] is False
    assert out1["reason"] == "execute_error"
    # Claim записан и НЕ снят (rollback после commit-а его не трогает).
    assert "executor_in_flight" in record.analysis
    assert "executor_applied" not in record.analysis

    # Ретрай, пока claim свежий → отказ, kubectl НЕ вызывается повторно.
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out2 = executor_apply.apply_intent(
            "inc-crash", "user2", _sig_for(_valid_intent_dict())
        )

    assert out2 == {"ok": False, "reason": "apply_in_flight"}
    mock_exec.assert_not_called()


def test_apply_refuses_on_fresh_in_flight_claim(mock_session):
    """Свежий executor_in_flight (конкурентный apply) → apply_in_flight."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_in_flight": {
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "claimed_by": "other-user",
        },
    })

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-inflight", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "apply_in_flight"}
    mock_exec.assert_not_called()


def test_apply_reclaims_stale_claim_without_second_write(mock_session):
    """Протухший claim: снимается (не висит вечно), но второго write НЕ даёт.

    Раньше переклейм молча гнал kubectl повторно: TTL claim-а 600s короче
    approval-окна 3600s, а факт первого write никто не проверял — инвариант
    «≤1 реальный write на одно одобрение» ломался. Теперь инцидент помечается
    «состояние кластера неизвестно → manual», и apply отказывает.
    """
    session, query = mock_session
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_in_flight": {"claimed_at": stale_ts, "claimed_by": "old-user"},
    })
    query.first.return_value = record

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent",
        side_effect=_fake_exec(),
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-reclaim", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is False
    assert out["reason"].startswith("cluster_state_unknown")
    # Ни dry-run, ни реального write — kubectl вообще не дёргаем.
    mock_exec.assert_not_called()
    assert "executor_applied" not in record.analysis
    # Claim не висит вечно: снят и заменён явной пометкой (закоммичен).
    assert "executor_in_flight" not in record.analysis
    marker = record.analysis["executor_state_unknown"]
    assert marker["stale_claim"]["claimed_by"] == "old-user"
    assert marker["resolution"] == "manual"
    assert session.commit.called


def test_apply_refuses_while_state_unknown_marker_present(mock_session):
    """Пометка cluster_state_unknown держит apply закрытым на старом approve."""
    _, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_state_unknown": {
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "stale_claim": {"claimed_by": "old-user"},
            "resolution": "manual",
        },
    })
    query.first.return_value = record

    # Approve выдан ДО пометки (обычный случай: клик → протухший claim).
    with _approved(age_seconds=120), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-unknown", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is False
    assert out["reason"].startswith("cluster_state_unknown")
    mock_exec.assert_not_called()


def test_apply_allows_after_reapprove_following_state_unknown(mock_session):
    """Единственный выход: approve, выданный ПОСЛЕ пометки (человек проверил кластер)."""
    _, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_state_unknown": {
            "detected_at": (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat(),
            "stale_claim": {"claimed_by": "old-user"},
            "resolution": "manual",
        },
    })
    query.first.return_value = record

    with _approved(age_seconds=30), patch.object(
        executor_apply.k8s_service, "execute_intent", side_effect=_fake_exec(),
    ):
        out = executor_apply.apply_intent(
            "inc-reapproved", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    assert "executor_applied" in record.analysis


def test_apply_refuses_on_unparseable_in_flight_claim(mock_session):
    """Битый claimed_at → fail-closed: считаем claim живым, отказ."""
    _, query = mock_session
    query.first.return_value = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "executor_in_flight": {"claimed_at": "not-a-timestamp"},
    })

    with _approved():
        out = executor_apply.apply_intent(
            "inc-badclaim", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "apply_in_flight"}


# ---------------------------------------------------------------------------
# Namespace-binding intent ↔ инцидент (#5)
# ---------------------------------------------------------------------------

def test_apply_refuses_namespace_mismatch(mock_session):
    """Intent целится в чужой namespace (галлюцинация/инъекция) → отказ."""
    _, query = mock_session
    foreign = {**_valid_intent_dict(), "namespace": "squad-7"}
    query.first.return_value = _make_record(
        {
            "execution_intent": foreign,
            "executor_result": {"status": "dry_run_ok"},
        },
        data={"namespace": "squad-1"},  # сам инцидент — из squad-1
    )

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent("inc-nsm", "user1", _sig_for(foreign))

    assert out == {"ok": False, "reason": "namespace_mismatch"}
    mock_exec.assert_not_called()


def test_apply_refuses_when_incident_namespace_unknown(mock_session):
    """У инцидента нет namespace — binding невозможен → fail-closed отказ."""
    _, query = mock_session
    query.first.return_value = _make_record(
        {
            "execution_intent": _valid_intent_dict(),
            "executor_result": {"status": "dry_run_ok"},
        },
        data={"labels": {}},
    )

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-nons", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "namespace_unbound"}
    mock_exec.assert_not_called()


def test_apply_accepts_namespace_from_labels_fallback(mock_session):
    """Namespace инцидента берётся из labels, если top-level поля нет."""
    _, query = mock_session
    query.first.return_value = _make_record(
        {
            "execution_intent": _valid_intent_dict(),
            "executor_result": {"status": "dry_run_ok"},
        },
        data={"labels": {"namespace": "squad-1"}},
    )

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent", side_effect=_fake_exec(),
    ):
        out = executor_apply.apply_intent(
            "inc-labels", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Свежесть самого intent-а + пере-dry-run перед write (M1)
# ---------------------------------------------------------------------------

def test_apply_refuses_stale_intent(mock_session):
    """«Approve & Run» на эмбеде недельной давности → intent_stale, без kubectl.

    Approve свежий, но план посчитан по состоянию кластера, которого уже нет.
    Якорь возраста — report_sent.sent_at (когда embed с кнопкой ушёл в Discord).
    """
    _, query = mock_session
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "report_sent": {"sent_at": week_ago, "attempts": 1},
    })
    query.first.return_value = record

    with _approved(age_seconds=30), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-oldintent", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is False
    assert out["reason"].startswith("intent_stale:")
    mock_exec.assert_not_called()
    assert "executor_in_flight" not in record.analysis


def test_apply_accepts_intent_within_max_age(mock_session):
    """Тот же якорь в пределах окна (час назад) — apply проходит."""
    _, query = mock_session
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
        "report_sent": {"sent_at": hour_ago, "attempts": 1},
    })
    query.first.return_value = record

    with _approved(age_seconds=30), patch.object(
        executor_apply.k8s_service, "execute_intent", side_effect=_fake_exec(),
    ):
        out = executor_apply.apply_intent(
            "inc-freshintent", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is True
    applied = record.analysis["executor_applied"]
    # Возраст плана и то, чем он измерен, попадают в provenance apply-а.
    assert applied["intent_time_anchor"] == "report_sent.sent_at"
    assert 3500 <= applied["intent_age_seconds"] <= 3700


def test_apply_refuses_when_intent_age_unknown(mock_session):
    """Ни одного якоря времени (нет маркеров прогона и created_at) → fail-closed."""
    _, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    record.created_at = None
    query.first.return_value = record

    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent"
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-noanchor", "user1", _sig_for(_valid_intent_dict())
        )

    assert out == {"ok": False, "reason": "intent_age_unknown"}
    mock_exec.assert_not_called()


def test_apply_refuses_when_pre_write_dry_run_fails(mock_session):
    """Свежий --dry-run=server упал (ресурса уже нет) → отказ, реального write нет."""
    session, query = mock_session
    record = _make_record({
        "execution_intent": _valid_intent_dict(),
        "executor_result": {"status": "dry_run_ok"},
    })
    query.first.return_value = record

    failed_dry_run = {
        "success": False,
        "stderr": 'Error from server (NotFound): deployments.apps "town-service" not found',
        "exit_code": 1,
        "dry_run": True,
    }
    with _approved(), patch.object(
        executor_apply.k8s_service, "execute_intent",
        side_effect=_fake_exec(dry_run_result=failed_dry_run),
    ) as mock_exec:
        out = executor_apply.apply_intent(
            "inc-recheck", "user1", _sig_for(_valid_intent_dict())
        )

    assert out["ok"] is False
    assert out["reason"].startswith("dry_run_recheck_failed:")
    assert "not found" in out["reason"]
    # Ровно один вызов, и он dry-run: реального write не было.
    assert mock_exec.call_count == 1
    assert mock_exec.call_args.kwargs["dry_run"] is True
    # Claim не ставился — read-only проверка не должна блокировать кнопку.
    assert "executor_in_flight" not in record.analysis
    assert "executor_applied" not in record.analysis
    session.commit.assert_not_called()
