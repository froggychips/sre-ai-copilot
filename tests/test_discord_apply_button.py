"""Тесты на Discord apply-flow (PR #3 executor track).

Не тестируем подпись Ed25519 — это покрывается signature-fixture в проде
и тестами verify_alertmanager_signature по аналогии. Здесь — handler-логика.

Что проверяем:
  - apply_{id} (шаг 1): возвращает ephemeral с двумя кнопками подтверждения,
    apply_intent НЕ вызывается.
  - apply_confirm_{id}: вызывает apply_intent в asyncio.to_thread, форматирует
    результат (success → ✅, failure → ❌).
  - apply_cancel_{id}: возвращает "Apply отменён", apply_intent не вызывается.
  - EXECUTOR_APPROVAL_ENABLED=false: apply-кнопки блокируются на handler-стороне.
  - refusal-reason → человекочитаемое сообщение (risk_too_high, already_applied и т.п.).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import discord_interactions


def _make_request_body(custom_id: str, user_id: str = "user-1") -> dict:
    """Минимальный Discord interaction payload."""
    return {
        "type": 3,  # MESSAGE_COMPONENT
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": user_id}},
    }


@pytest.mark.asyncio
async def test_apply_step1_shows_confirm_buttons():
    """apply_{id} → ephemeral с двумя кнопками; ничего не выполняется."""
    payload = _make_request_body("apply_inc-1")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
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
async def test_apply_confirm_calls_apply_intent_and_formats_success():
    payload = _make_request_body("apply_confirm_inc-2", user_id="user-42")

    fake_result = {
        "ok": True,
        "result": {
            "success": True,
            "command": "kubectl rollout restart deployment/town-service -n squad-1",
            "stdout": "deployment.apps/town-service restarted",
        },
    }

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("app.services.executor_apply.apply_intent", return_value=fake_result) as mock_apply:
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    mock_apply.assert_called_once_with("inc-2", "user-42")
    content = resp["data"]["content"]
    assert "✅" in content
    assert "town-service restarted" in content
    assert "kubectl rollout restart" in content


@pytest.mark.asyncio
async def test_apply_confirm_formats_kubectl_failure():
    payload = _make_request_body("apply_confirm_inc-3")
    fake_result = {
        "ok": True,
        "result": {
            "success": False,
            "command": "kubectl rollout restart deployment/x -n squad-1",
            "stderr": "Error from server: deployment 'x' not found",
        },
    }

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("app.services.executor_apply.apply_intent", return_value=fake_result):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    content = resp["data"]["content"]
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
async def test_apply_confirm_refusal_messages(reason, expected_phrase):
    payload = _make_request_body("apply_confirm_inc-x")
    fake = {"ok": False, "reason": reason}

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch("app.services.executor_apply.apply_intent", return_value=fake):
        request = MagicMock()
        request.body = AsyncMock(return_value=__import__("json").dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0"
        )

    assert expected_phrase in resp["data"]["content"]


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
