"""Тесты на Discord apply-flow (PR #3 executor + PR #4 deferred response).

Не тестируем подпись Ed25519 — покрывается signature-fixture в проде и
тестами verify_alertmanager_signature по аналогии. Здесь — handler-логика.

Что проверяем:
  - apply_{id} (шаг 1): ephemeral с двумя кнопками подтверждения, apply_intent НЕ вызывается.
  - apply_confirm_{id}: handler возвращает type=5 (deferred) СРАЗУ;
    _apply_in_background (отдельный тест) запускает apply_intent в to_thread и
    шлёт PATCH followup с финальным сообщением (success/failure/refusal).
  - apply_cancel_{id}: ephemeral "Apply отменён", apply_intent не вызывается.
  - EXECUTOR_APPROVAL_ENABLED=false: apply-кнопки блокируются на handler-стороне.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import discord_interactions


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
    """apply_{id} → ephemeral с двумя кнопками; ничего не выполняется."""
    payload = _make_request_body("apply_inc-1")

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
    assert {c["custom_id"] for c in components} == {
        "apply_confirm_inc-1",
        "apply_cancel_inc-1",
    }


@pytest.mark.asyncio
async def test_apply_step1_blocked_when_approval_disabled():
    """EXECUTOR_APPROVAL_ENABLED=false → отказ на шаге 1."""
    payload = _make_request_body("apply_inc-1")

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
async def test_apply_confirm_returns_deferred_response():
    """Handler СРАЗУ возвращает type=5 (deferred); apply работает в background-task."""
    payload = _make_request_body("apply_confirm_inc-2", user_id="user-42")

    captured_task = []

    def spy_create_task(coro):
        captured_task.append(coro)
        # Не запускаем — отменяем coroutine чтобы не было warning-а про non-awaited.
        coro.close()
        # Возвращаем dummy future
        loop = __import__("asyncio").get_running_loop()
        f = loop.create_future()
        f.set_result(None)
        return f

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("asyncio.create_task", side_effect=spy_create_task):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    # Discord type=5 (DEFERRED_CHANNEL_MESSAGE) + ephemeral flag
    assert resp["type"] == 5
    assert resp["data"]["flags"] == 64
    # background task была запущена
    assert len(captured_task) == 1


@pytest.mark.asyncio
async def test_apply_confirm_without_token_returns_immediate_error():
    """Без interaction.token не сможем сделать followup — fail-fast."""
    payload = _make_request_body("apply_confirm_inc-y", token="")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert resp["type"] == 4  # immediate response, не deferred
    assert "token отсутствует" in resp["data"]["content"]


@pytest.mark.asyncio
async def test_apply_in_background_success_sends_followup():
    """_apply_in_background → apply_intent OK → _send_followup с ✅."""
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
        await discord_interactions._apply_in_background("inc-2", "user-42", "tok-abc")

    mock_apply.assert_called_once_with("inc-2", "user-42")
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
        await discord_interactions._apply_in_background("inc-3", "u", "tok")

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
])
async def test_apply_in_background_refusal_followups(reason, expected_phrase):
    fake = {"ok": False, "reason": reason}
    sent = []
    async def fake_followup(token, content):
        sent.append((token, content))

    with patch.object(discord_interactions, "_send_followup", new=fake_followup), \
         patch("app.services.executor_apply.apply_intent", return_value=fake):
        await discord_interactions._apply_in_background("inc-x", "u", "tok")

    _, content = sent[0]
    assert expected_phrase in content


@pytest.mark.asyncio
async def test_apply_cancel_does_not_call_apply_intent():
    payload = _make_request_body("apply_cancel_inc-x")

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
