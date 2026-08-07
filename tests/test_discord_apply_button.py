"""Тесты на Discord apply-flow (PR #3 executor + PR #4 deferred response).

Не тестируем подпись Ed25519 — покрывается signature-fixture в проде и
тестами verify_alertmanager_signature по аналогии. Здесь — handler-логика.

custom_id apply-пути — colon-формат с подписью (как approve/decline):
  - apply:{id}:{sig}          → шаг 1, ephemeral с кнопками подтверждения.
  - apply_confirm:{id}:{sig}  → шаг 2, записывает ActionApproval(approved) и
    возвращает type=5 (deferred); apply работает в background-task.
  - apply_cancel:{id}         → ephemeral "Apply отменён".
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import discord_interactions

_SIG = "abc123def456"


def _make_request_body(custom_id: str, user_id: str = "user-1", token: str = "tok-abc") -> dict:
    """Минимальный Discord interaction payload."""
    return {
        "type": 3,  # MESSAGE_COMPONENT
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": user_id}},
        "token": token,
    }


@pytest.mark.asyncio
async def test_apply_step1_shows_confirm_buttons():
    """apply:{id}:{sig} → ephemeral с двумя кнопками; ничего не выполняется."""
    payload = _make_request_body(f"apply:inc-1:{_SIG}")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    data = resp["data"]
    assert "Запустить **kubectl**" in data["content"]
    components = data["components"][0]["components"]
    # Подпись протаскивается в confirm-кнопку; cancel — без подписи.
    assert {c["custom_id"] for c in components} == {
        f"apply_confirm:inc-1:{_SIG}",
        "apply_cancel:inc-1",
    }


@pytest.mark.asyncio
async def test_apply_step1_blocked_when_approval_disabled():
    """EXECUTOR_APPROVAL_ENABLED=false → отказ на шаге 1."""
    payload = _make_request_body(f"apply:inc-1:{_SIG}")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", False):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert "EXECUTOR_APPROVAL_ENABLED=false" in resp["data"]["content"]


@pytest.mark.asyncio
async def test_apply_confirm_blocked_when_approval_disabled():
    """EXECUTOR_APPROVAL_ENABLED=false → шаг 2 тоже fail-closed: ни записи
    approval, ни background-task (кнопка могла остаться в старом сообщении)."""
    payload = _make_request_body(f"apply_confirm:inc-off:{_SIG}")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", False), \
         patch.object(discord_interactions, "_record_decision") as mock_record, \
         patch("asyncio.create_task") as mock_task:
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert "EXECUTOR_APPROVAL_ENABLED=false" in resp["data"]["content"]
    mock_record.assert_not_called()
    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_apply_confirm_records_approval_and_returns_deferred():
    """apply_confirm → пишет ActionApproval(approved), СРАЗУ type=5; apply в background."""
    payload = _make_request_body(f"apply_confirm:inc-2:{_SIG}", user_id="user-42")

    captured_task = []

    def spy_create_task(coro):
        captured_task.append(coro)
        coro.close()  # не запускаем — гасим coroutine
        loop = __import__("asyncio").get_running_loop()
        f = loop.create_future()
        f.set_result(None)
        return f

    rec_calls = []

    def fake_record(incident_id, sig, status, approved_by):
        rec_calls.append((incident_id, sig, status, approved_by))
        return {"already_decided": False, "status": status,
                "approved_by": approved_by, "decided_at": "12:00 UTC"}

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions, "_record_decision", side_effect=fake_record), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("asyncio.create_task", side_effect=spy_create_task):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert resp["type"] == 5
    assert resp["data"]["flags"] == 64
    assert len(captured_task) == 1
    # Одобрение записано как approved для (incident, sig).
    assert rec_calls == [("inc-2", _SIG, "approved", "user-42")]


@pytest.mark.asyncio
async def test_apply_confirm_declined_collision_aborts():
    """Если по (incident, sig) уже decline — apply отменяется, background не стартует."""
    payload = _make_request_body(f"apply_confirm:inc-d:{_SIG}", user_id="user-42")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions, "_record_decision",
                      return_value={"already_decided": True, "status": "declined",
                                    "approved_by": "bob", "decided_at": "11:00 UTC"}), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("asyncio.create_task") as mock_task:
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert resp["type"] == 4
    assert "declined" in resp["data"]["content"]
    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_apply_confirm_without_token_returns_immediate_error():
    """Без interaction.token не сможем сделать followup — fail-fast до записи approval."""
    payload = _make_request_body(f"apply_confirm:inc-y:{_SIG}", token="")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions, "_record_decision") as mock_record, \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert resp["type"] == 4  # immediate response, не deferred
    assert "token отсутствует" in resp["data"]["content"]
    mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_apply_in_background_success_sends_followup():
    """_apply_in_background → apply_intent OK → _send_followup с ✅; sig прокинут."""
    fake_result = {
        "ok": True,
        "result": {
            "success": True,
            "command": "kubectl rollout restart deployment/town-service -n squad-1",
            "stdout": "deployment.apps/town-service restarted",
        },
    }
    sent = []

    async def fake_followup(token, content):
        sent.append((token, content))

    with patch.object(discord_interactions, "_send_followup", new=fake_followup), \
         patch("app.services.executor_apply.apply_intent", return_value=fake_result) as mock_apply:
        await discord_interactions._apply_in_background("inc-2", "user-42", _SIG, "tok-abc")

    mock_apply.assert_called_once_with("inc-2", "user-42", _SIG)
    assert len(sent) == 1
    token, content = sent[0]
    assert token == "tok-abc"
    assert "✅" in content
    assert "town-service restarted" in content


@pytest.mark.asyncio
async def test_apply_in_background_failure_sends_followup_with_error():
    fake_result = {
        "ok": True,
        "result": {
            "success": False,
            "command": "kubectl rollout restart deployment/x -n squad-1",
            "stderr": "Error from server: deployment 'x' not found",
        },
    }
    sent = []
    async def fake_followup(token, content):
        sent.append((token, content))

    with patch.object(discord_interactions, "_send_followup", new=fake_followup), \
         patch("app.services.executor_apply.apply_intent", return_value=fake_result):
        await discord_interactions._apply_in_background("inc-3", "u", _SIG, "tok")

    _, content = sent[0]
    assert "❌" in content
    assert "not found" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected_phrase", [
    ("already_applied",          "Уже применено"),
    ("no_intent",                "Нет ExecutionIntent"),
    ("dry_run_not_ok:guardrail_blocked", "dry-run не прошёл"),
    ("risk_too_high:high",       "Action risk=`high`"),
    ("incident_not_found",       "не найден"),
    ("not_approved",             "Нет записи об одобрении"),
    ("signature_mismatch",       "изменилось"),
])
async def test_apply_in_background_refusal_followups(reason, expected_phrase):
    fake = {"ok": False, "reason": reason}
    sent = []
    async def fake_followup(token, content):
        sent.append((token, content))

    with patch.object(discord_interactions, "_send_followup", new=fake_followup), \
         patch("app.services.executor_apply.apply_intent", return_value=fake):
        await discord_interactions._apply_in_background("inc-x", "u", _SIG, "tok")

    _, content = sent[0]
    assert expected_phrase in content


@pytest.mark.asyncio
async def test_apply_cancel_does_not_call_apply_intent():
    payload = _make_request_body("apply_cancel:inc-x")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch("app.services.executor_apply.apply_intent") as mock_apply:
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    mock_apply.assert_not_called()
    assert resp["data"]["content"] == "Apply отменён."
