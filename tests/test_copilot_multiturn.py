"""Регрессии multi-turn /copilot (celery_worker._generate_reply_logic).

До фикса диалог умирал после первого же ответа:
  * каждый прогон generate_reply начинается с transition(INVESTIGATING),
    а успешный прогон оставляет FIX_PROPOSED. В TRANSITIONS[FIX_PROPOSED]
    не было INVESTIGATING → второе сообщение всегда падало на invalid
    transition и уводило state в FAILED (терминал);
  * except-ветка писала FAILED прямым присваиванием ПОВЕРХ терминальных
    состояний и коммитила без rollback (abort-транзакция →
    PendingRollbackError маскировал первопричину);
  * SessionLocal() + первый query стояли ДО try/finally — OperationalError
    на query утекал мимо db.close() (утечка коннекта из пула);
  * generate_reply ретраил детерминированные ошибки (Conversation not
    found, invalid transition), заведомо мёртвые после первого фейла.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from app.celery_worker import _generate_reply_logic, generate_reply
from app.core.state_machine import IncidentState


# MARK: - Helpers

def _mock_session(conv):
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = conv
    return session


def _conv(state: IncidentState) -> MagicMock:
    conv = MagicMock()
    conv.current_state = state.value
    return conv


def _mock_llm_layer(mocker, analysis: str):
    """Мокаем LLM-слой по образцу tests/test_state_transitions.py:
    патчим в namespace celery_worker (туда они импортированы)."""
    analyzer = MagicMock()
    analyzer.analyze = AsyncMock(return_value=analysis)
    mocker.patch("app.celery_worker.AnalyzerAgent", return_value=analyzer)
    builder = MagicMock()
    builder.build_context = AsyncMock(return_value={})
    mocker.patch("app.celery_worker.ContextBuilder", return_value=builder)
    return analyzer


# MARK: - Multi-turn: второй прогон на FIX_PROPOSED

@pytest.mark.asyncio
async def test_second_turn_on_fix_proposed_succeeds(mocker):
    """Follow-up сообщение в диалог, оставшийся в FIX_PROPOSED после первого
    ответа, НЕ падает на transition(INVESTIGATING) и завершается штатно."""
    conv = _conv(IncidentState.FIX_PROPOSED)
    session = _mock_session(conv)
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)
    analysis = json.dumps({"confidence_score": 0.9, "summary": "ok"})
    _mock_llm_layer(mocker, analysis)

    result = await _generate_reply_logic("conv-1", "а что с памятью ноды?")

    assert result == analysis
    # INVESTIGATING → HYPOTHESIS_GENERATED → FIX_PROPOSED: диалог снова
    # готов к следующему follow-up-у, а не FAILED.
    assert conv.current_state == IncidentState.FIX_PROPOSED.value
    session.close.assert_called_once()


# MARK: - except-ветка: терминальные состояния не перезаписываются

@pytest.mark.parametrize(
    "terminal",
    [IncidentState.RESOLVED, IncidentState.TRIAGE_REQUIRED, IncidentState.FAILED],
)
@pytest.mark.asyncio
async def test_except_branch_does_not_overwrite_terminal_state(mocker, terminal):
    conv = _conv(terminal)
    session = _mock_session(conv)
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)

    # Из терминала transition(INVESTIGATING) невалиден → детерминированный
    # ValueError; except-ветка НЕ должна переписать состояние в FAILED.
    with pytest.raises(ValueError, match="Invalid transition"):
        await _generate_reply_logic("conv-1", "prompt")

    assert conv.current_state == terminal.value
    session.close.assert_called_once()


@pytest.mark.asyncio
async def test_failed_written_after_rollback_on_nonterminal(mocker):
    """Нетерминальный диалог при сбое уходит в FAILED, но commit-у
    предшествует rollback (abort-транзакция не должна маскировать
    первопричину PendingRollbackError-ом)."""
    conv = _conv(IncidentState.OPEN)
    session = _mock_session(conv)
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)
    _mock_llm_layer(mocker, "unused")
    mocker.patch(
        "app.celery_worker.AnalyzerAgent",
        side_effect=RuntimeError("LLM exploded"),
    )

    with pytest.raises(RuntimeError, match="LLM exploded"):
        await _generate_reply_logic("conv-1", "prompt")

    assert conv.current_state == IncidentState.FAILED.value
    names = [name for name, _args, _kwargs in session.method_calls]
    last_commit = max(i for i, n in enumerate(names) if n == "commit")
    assert names.index("rollback") < last_commit, (
        "rollback должен идти ДО commit-а FAILED"
    )
    session.close.assert_called_once()


# MARK: - Утечка сессии: query внутри try

@pytest.mark.asyncio
async def test_session_closed_when_first_query_fails(mocker):
    session = MagicMock()
    session.query.side_effect = OperationalError(
        "SELECT 1", {}, Exception("pg down")
    )
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)

    with pytest.raises(OperationalError):
        await _generate_reply_logic("conv-1", "prompt")

    # Раньше query стоял ДО try/finally и OperationalError утекал мимо
    # db.close() — коннект из пула не возвращался.
    session.close.assert_called_once()


# MARK: - generate_reply: retry только для транзиентных ошибок

def test_generate_reply_no_retry_on_deterministic_error(mocker):
    """Conversation not found — детерминированная ошибка: повтор заведомо
    мёртв, self.retry не должен вызываться."""
    session = _mock_session(None)
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)
    retry_mock = mocker.patch.object(generate_reply, "retry")

    with pytest.raises(ValueError, match="not found"):
        generate_reply("conv-missing", "prompt")

    retry_mock.assert_not_called()


def test_generate_reply_retries_transient_error(mocker):
    """OperationalError (БД моргнула) — транзиентная ошибка: ретраится."""
    session = MagicMock()
    session.query.side_effect = OperationalError(
        "SELECT 1", {}, Exception("pg down")
    )
    mocker.patch("app.celery_worker.SessionLocal", return_value=session)
    retry_mock = mocker.patch.object(
        generate_reply, "retry", side_effect=Retry("retrying")
    )

    with pytest.raises(Retry):
        generate_reply("conv-1", "prompt")

    retry_mock.assert_called_once()
