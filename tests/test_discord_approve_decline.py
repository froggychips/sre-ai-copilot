"""Тесты на Discord approve/decline buttons для proposed actions.

См. app/api/discord_interactions.py:
  - approve:{incident_id}:{intent_signature}
  - decline:{incident_id}:{intent_signature}

Проверяем:
  - intent_signature детерминирован (один intent → один sig)
  - approve пишет запись в kg_action_approvals со status=approved
  - decline пишет запись со status=declined
  - повторный клик → already_decided + audit-event не дублируется
  - EXECUTOR_ENABLED=true → fire-and-forget executor task
  - EXECUTOR_ENABLED=false → ack без запуска
  - send_incident_report добавляет approve_row ТОЛЬКО при наличии bot-config
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import discord_interactions
from app.core.execution_dsl import ActionType, ExecutionIntent
from app.database import Base
from app.services.intent_signature import compute_signature


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite + Base.metadata.create_all
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db(monkeypatch):
    """In-memory SQLite + create_all для ActionApproval. Подменяет SessionLocal."""
    # Импорт здесь чтобы Base зарегистрировала ActionApproval до create_all
    from app.knowledge_graph import schema  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    monkeypatch.setattr(discord_interactions, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


def _make_payload(custom_id: str, user_id: str = "u-1", user_name: str = "yaroslav",
                  message_id: str = "msg-1", channel_id: str = "ch-1") -> dict:
    return {
        "type": 3,  # MESSAGE_COMPONENT
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": user_id, "username": user_name}},
        "token": "tok-abc",
        "message": {
            "id": message_id,
            "channel_id": channel_id,
            "embeds": [{
                "title": "test",
                "footer": {"text": "incident/inc-1"},
            }],
        },
        "channel_id": channel_id,
    }


def _intent() -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        risk="medium",
    )


# ---------------------------------------------------------------------------
# Intent signature
# ---------------------------------------------------------------------------

def test_intent_signature_is_deterministic():
    sig1 = compute_signature(_intent())
    sig2 = compute_signature(_intent())
    assert sig1 == sig2
    assert len(sig1) == 12


def test_intent_signature_differs_for_different_intents():
    a = _intent()
    b = ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="OTHER",
        namespace="squad-1",
        risk="medium",
    )
    assert compute_signature(a) != compute_signature(b)


def test_intent_signature_ignores_risk():
    """Risk не часть signature — пользователь подтверждает суть, не пометку рисков."""
    low = _intent()
    low.risk = "low"
    high = _intent()
    high.risk = "high"
    assert compute_signature(low) == compute_signature(high)


# ---------------------------------------------------------------------------
# Approve flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_records_decision_and_acks(isolated_db):
    """Approve пишет row в kg_action_approvals + audit event + ack-ответ."""
    intent_sig = compute_signature(_intent())
    payload = _make_payload(f"approve:inc-1:{intent_sig}", user_name="yar")
    audit_calls = []

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_ENABLED", False), \
         patch.object(discord_interactions.audit_service, "log_event",
                      side_effect=lambda et, d: audit_calls.append((et, d))), \
         patch("asyncio.create_task"):
        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert resp["type"] == 4
    assert "Approved" in resp["data"]["content"]
    assert "yar" in resp["data"]["content"]
    # EXECUTOR_ENABLED=false — должно явно говорить про "when executor goes live"
    assert "goes live" in resp["data"]["content"] or "EXECUTOR_ENABLED=false" in resp["data"]["content"]
    assert len(audit_calls) == 1
    assert audit_calls[0][0] == "INCIDENT_ACTION_APPROVED"
    assert audit_calls[0][1]["intent_signature"] == intent_sig

    # Запись действительно в БД
    from app.knowledge_graph.schema import ActionApproval
    with isolated_db() as session:
        rows = session.query(ActionApproval).all()
        assert len(rows) == 1
        assert rows[0].status == "approved"
        assert rows[0].approved_by == "yar"


@pytest.mark.asyncio
async def test_decline_records_decision_and_acks(isolated_db):
    intent_sig = compute_signature(_intent())
    payload = _make_payload(f"decline:inc-1:{intent_sig}", user_name="zakhar")
    audit_calls = []

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.audit_service, "log_event",
                      side_effect=lambda et, d: audit_calls.append((et, d))), \
         patch("asyncio.create_task"):
        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert resp["type"] == 4
    assert "Declined" in resp["data"]["content"]
    assert "zakhar" in resp["data"]["content"]
    assert audit_calls[0][0] == "INCIDENT_ACTION_DECLINED"

    from app.knowledge_graph.schema import ActionApproval
    with isolated_db() as session:
        rows = session.query(ActionApproval).all()
        assert len(rows) == 1
        assert rows[0].status == "declined"


@pytest.mark.asyncio
async def test_double_click_returns_already_decided(isolated_db):
    """Повторный клик ловится UNIQUE — возвращаем already-message, не повторно audit-им."""
    intent_sig = compute_signature(_intent())
    payload = _make_payload(f"approve:inc-1:{intent_sig}", user_name="first")
    audit_calls = []

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_ENABLED", False), \
         patch.object(discord_interactions.audit_service, "log_event",
                      side_effect=lambda et, d: audit_calls.append((et, d))), \
         patch("asyncio.create_task"):
        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        # Первый клик
        resp1 = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )
        # Второй клик (от другого пользователя) — same incident+intent
        payload2 = _make_payload(f"approve:inc-1:{intent_sig}", user_name="second")
        request2 = MagicMock()
        request2.body = AsyncMock(return_value=json.dumps(payload2).encode())
        resp2 = await discord_interactions.discord_interactions(
            request2, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert "Approved" in resp1["data"]["content"]
    assert "Already approved" in resp2["data"]["content"]
    assert "first" in resp2["data"]["content"]
    # Audit получил только ОДИН event — повторный клик не дублирует
    assert len(audit_calls) == 1


@pytest.mark.asyncio
async def test_approve_executor_enabled_dispatches_task(isolated_db):
    """EXECUTOR_ENABLED=true → fire-and-forget executor task."""
    intent_sig = compute_signature(_intent())
    payload = _make_payload(f"approve:inc-X:{intent_sig}", user_name="user")

    created_tasks: list = []

    def spy(coro):
        created_tasks.append(coro)
        if hasattr(coro, "close"):
            coro.close()
        import asyncio
        loop = asyncio.get_running_loop()
        f = loop.create_future()
        f.set_result(None)
        return f

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_ENABLED", True), \
         patch.object(discord_interactions.audit_service, "log_event"), \
         patch("asyncio.create_task", side_effect=spy):
        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    # Минимум 1 create_task — для edit_message_after_decision;
    # при EXECUTOR_ENABLED=true ещё один — apply_intent fire-and-forget.
    assert len(created_tasks) >= 2
    assert "Executor launched" in resp["data"]["content"]


@pytest.mark.asyncio
async def test_malformed_custom_id_rejected(isolated_db):
    """approve без двух двоеточий → ошибка формата."""
    payload = _make_payload("approve:only-one-part")

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"):
        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps(payload).encode())
        resp = await discord_interactions.discord_interactions(
            request, x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert "Неверный формат" in resp["data"]["content"]


# ---------------------------------------------------------------------------
# send_incident_report → approve_row только при bot-config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_incident_report_skips_approve_row_when_bot_not_configured(monkeypatch):
    """Без DISCORD_BOT_TOKEN+CHANNEL_ID — approve_row не добавляется (webhook не поддерживает)."""
    from app.services.discord_service import DiscordService

    monkeypatch.setattr("app.config.settings.DISCORD_DRY_RUN", False)
    monkeypatch.setattr("app.config.settings.DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr("app.config.settings.DISCORD_INCIDENT_CHANNEL_ID", None)
    monkeypatch.setattr("app.config.settings.DISCORD_WEBHOOK_URL", "https://example/test")
    monkeypatch.setattr("app.config.settings.EXECUTOR_APPROVAL_ENABLED", False)

    captured = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured.append({"url": url, "json": json, "headers": headers})
            r = MagicMock()
            r.status_code = 200
            return r

    svc = DiscordService()
    with patch("httpx.AsyncClient", return_value=FakeClient()):
        await svc.send_incident_report(
            incident_id="inc-1",
            alertname="TestAlert",
            namespace="squad-1",
            pod=None, service="town-service", node=None,
            severity="warning",
            cause="test",
            resolution_quality="unresolved",
            synthesis="something",
            execution_intent=_intent(),
            executor_result=None,
        )

    assert len(captured) == 1
    components = captured[0]["json"]["components"]
    # Только feedback-row, без approve-row
    assert len(components) == 1
    custom_ids = {c["custom_id"] for c in components[0]["components"]}
    assert all(not cid.startswith("approve:") for cid in custom_ids)
    assert all(not cid.startswith("decline:") for cid in custom_ids)


@pytest.mark.asyncio
async def test_send_incident_report_uses_bot_api_when_configured(monkeypatch):
    """При bot-config + intent — шлём через bot API с approve/decline кнопками."""
    from app.services.discord_service import DiscordService

    monkeypatch.setattr("app.config.settings.DISCORD_DRY_RUN", False)
    monkeypatch.setattr("app.config.settings.DISCORD_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr("app.config.settings.DISCORD_INCIDENT_CHANNEL_ID", "1501861363880824943")
    monkeypatch.setattr("app.config.settings.DISCORD_WEBHOOK_URL", "https://example/test")
    monkeypatch.setattr("app.config.settings.EXECUTOR_APPROVAL_ENABLED", False)

    captured = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured.append({"url": url, "json": json, "headers": headers})
            r = MagicMock()
            r.status_code = 200
            return r

    svc = DiscordService()
    with patch("httpx.AsyncClient", FakeClient):
        await svc.send_incident_report(
            incident_id="inc-1",
            alertname="TestAlert",
            namespace="squad-1",
            pod=None, service="town-service", node=None,
            severity="warning",
            cause="test",
            resolution_quality="unresolved",
            synthesis="something",
            execution_intent=_intent(),
            executor_result=None,
        )

    # Один POST — через bot API
    assert len(captured) == 1
    assert "/channels/1501861363880824943/messages" in captured[0]["url"]
    assert captured[0]["headers"]["Authorization"] == "Bot test-bot-token"
    components = captured[0]["json"]["components"]
    # 2 row: feedback + approve/decline
    assert len(components) == 2
    approve_row = components[1]["components"]
    custom_ids = {c["custom_id"] for c in approve_row}
    expected_sig = compute_signature(_intent())
    assert f"approve:inc-1:{expected_sig}" in custom_ids
    assert f"decline:inc-1:{expected_sig}" in custom_ids
