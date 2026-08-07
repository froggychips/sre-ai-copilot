"""Authz на Discord feedback-кнопках (👍/👎).

Раньше feedback_pos_* / feedback_neg_confirm_* писали is_accepted /
user_feedback без какой-либо авторизации — любой участник гильдии мог
отравить accuracy-статистику. Теперь — тот же fail-closed whitelist
(_is_authorized_approver), что и на apply/approve-путях.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import discord_interactions


def _payload(custom_id: str, user_id: str = "u-1", roles: list | None = None) -> dict:
    member: dict = {"user": {"id": user_id, "username": "tester"}}
    if roles is not None:
        member["roles"] = roles
    return {
        "type": 3,  # MESSAGE_COMPONENT
        "data": {"custom_id": custom_id},
        "member": member,
        "token": "tok-abc",
    }


async def _click(payload: dict):
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(payload).encode())
    return await discord_interactions.discord_interactions(
        request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
    )


def _env(user_ids: str = "u-1", role_ids: str = ""):
    return [
        patch.object(discord_interactions, "_verify_signature", return_value=True),
        patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"),
        patch.object(discord_interactions.settings, "DISCORD_APPROVERS_USER_IDS", user_ids),
        patch.object(discord_interactions.settings, "DISCORD_APPROVERS_ROLE_IDS", role_ids),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_id", [
    "feedback_pos_inc-1",
    "feedback_neg_inc-1",
    "feedback_neg_confirm_inc-1",
])
async def test_unauthorized_feedback_denied_and_not_stored(custom_id):
    """Не в whitelist → отказ, _store_feedback не вызывается, denial аудируется."""
    audit_calls = []
    patches = _env(user_ids="someone-else")
    with patches[0], patches[1], patches[2], patches[3], \
         patch.object(discord_interactions, "_store_feedback") as mock_store, \
         patch.object(discord_interactions.audit_service, "log_event",
                      side_effect=lambda et, d: audit_calls.append((et, d))):
        resp = await _click(_payload(custom_id, user_id="rando"))

    assert resp["data"]["flags"] == 64  # ephemeral
    assert "not authorized" in resp["data"]["content"].lower()
    mock_store.assert_not_called()
    assert audit_calls[0][0] == "DISCORD_FEEDBACK_DENIED_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_empty_whitelists_fail_closed_for_feedback():
    """Оба списка пусты → fail-closed (как на approve-пути)."""
    patches = _env(user_ids="", role_ids="")
    with patches[0], patches[1], patches[2], patches[3], \
         patch.object(discord_interactions, "_store_feedback") as mock_store, \
         patch.object(discord_interactions.audit_service, "log_event"):
        resp = await _click(_payload("feedback_pos_inc-1"))

    assert "not authorized" in resp["data"]["content"].lower()
    mock_store.assert_not_called()


@pytest.mark.asyncio
async def test_authorized_user_positive_feedback_stored():
    patches = _env(user_ids="u-1")
    with patches[0], patches[1], patches[2], patches[3], \
         patch.object(discord_interactions, "_store_feedback",
                      return_value=True) as mock_store, \
         patch.object(discord_interactions.audit_service, "log_event"):
        resp = await _click(_payload("feedback_pos_inc-1", user_id="u-1"))

    mock_store.assert_called_once_with("inc-1", "positive", "u-1")
    assert "верное решение" in resp["data"]["content"]


@pytest.mark.asyncio
async def test_authorized_via_role_negative_confirm_stored():
    patches = _env(user_ids="", role_ids="sre-role")
    with patches[0], patches[1], patches[2], patches[3], \
         patch.object(discord_interactions, "_store_feedback",
                      return_value=True) as mock_store, \
         patch.object(discord_interactions.audit_service, "log_event"):
        resp = await _click(_payload(
            "feedback_neg_confirm_inc-2", user_id="rando", roles=["sre-role"],
        ))

    mock_store.assert_called_once_with("inc-2", "negative", "rando")
    assert "ошибочный анализ" in resp["data"]["content"]


@pytest.mark.asyncio
async def test_feedback_cancel_needs_no_authz():
    """Отмена ничего не пишет — доступна без whitelist (UX)."""
    patches = _env(user_ids="someone-else")
    with patches[0], patches[1], patches[2], patches[3], \
         patch.object(discord_interactions, "_store_feedback") as mock_store:
        resp = await _click(_payload("feedback_neg_cancel_inc-1", user_id="rando"))

    assert resp["data"]["content"] == "Отменено."
    mock_store.assert_not_called()
