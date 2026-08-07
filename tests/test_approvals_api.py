"""Approvals API: честные статус-коды вместо безусловного 200.

Раньше approve/reject игнорировали None от атомарного перехода
(NOT_FOUND/NOT_PENDING) и всегда отвечали «Action approved/rejected» —
оператор, «отклонивший» уже одобренное действие, был уверен что оно
отклонено. Плюс GET /approvals/{id} отдавал kubectl-команду и risk без
проверки роли approver.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import approvals
from app.auth import User


def _approver() -> User:
    return User(sub="u1", email="a@example.com", roles=["approver"])


def _viewer() -> User:
    return User(sub="u2", email="v@example.com", roles=["viewer"])


@pytest.mark.asyncio
async def test_approve_success_returns_new_status():
    with patch.object(approvals.approval_manager, "approve",
                      new=AsyncMock(return_value="APPROVED")):
        out = await approvals.approve_action("id-1", user=_approver())
    assert out["message"] == "Action approved"
    assert out["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_reject_on_already_approved_returns_409():
    """Переход не случился (уже APPROVED) → 409 с реальным статусом, не 200."""
    with patch.object(approvals.approval_manager, "reject",
                      new=AsyncMock(return_value=None)), \
         patch.object(approvals.approval_manager, "get_status",
                      new=AsyncMock(return_value="APPROVED")):
        with pytest.raises(HTTPException) as exc:
            await approvals.reject_action("id-1", user=_approver())
    assert exc.value.status_code == 409
    assert "APPROVED" in exc.value.detail


@pytest.mark.asyncio
async def test_approve_on_expired_returns_404():
    with patch.object(approvals.approval_manager, "approve",
                      new=AsyncMock(return_value=None)), \
         patch.object(approvals.approval_manager, "get_status",
                      new=AsyncMock(return_value="EXPIRED")):
        with pytest.raises(HTTPException) as exc:
            await approvals.approve_action("id-gone", user=_approver())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_requires_approver_role():
    with pytest.raises(HTTPException) as exc:
        await approvals.approve_action("id-1", user=_viewer())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_details_requires_approver_role():
    """Детали (kubectl-команда + risk) больше не утекают любому авторизованному."""
    with patch.object(approvals.approval_manager, "get_details",
                      new=AsyncMock(return_value={"id": "id-1"})) as mock_details:
        with pytest.raises(HTTPException) as exc:
            await approvals.get_approval_details("id-1", user=_viewer())
    assert exc.value.status_code == 403
    mock_details.assert_not_called()


@pytest.mark.asyncio
async def test_get_details_ok_for_approver():
    with patch.object(approvals.approval_manager, "get_details",
                      new=AsyncMock(return_value={"id": "id-1", "status": "PENDING"})):
        out = await approvals.get_approval_details("id-1", user=_approver())
    assert out["id"] == "id-1"
