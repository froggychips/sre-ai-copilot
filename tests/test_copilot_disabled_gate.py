"""`/copilot`, `/replay/{id}` и `generate_reply` при выключенном LLM-контуре.

До 18.11.2026 флаг `LLM_PIPELINE_ENABLED` закрывал только `process_incident`.
Ручка `/copilot` его не смотрела: создавала диалог, писала сообщение, ставила
`generate_reply` в очередь и отдавала 202 с task_id. В проде
(`LLM_BACKEND=claude_cli`, бинарника в образе нет) такая задача могла только
упасть и трижды уйти в retry — а клиент ждал ответа, которого не будет.

Теперь при выключенном флаге:
  * `/copilot` и `/replay/{id}` отвечают 503 ДО любых побочных эффектов;
  * `generate_reply` завершается сразу, без БД, LLM и ретраев — на случай
    сообщений, уже лежащих в очереди, и расхождения настроек api/worker в
    окне выкатки.

conftest по умолчанию ВКЛЮЧАЕТ флаг для всех тестов, поэтому каждый тест
здесь выставляет его явно.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import User, get_current_user
from app.config import settings

# ── generate_reply при выключенном флаге ─────────────────────────────────


def test_generate_reply_disabled_returns_without_work_or_retry(mocker) -> None:
    from app.celery_worker import generate_reply

    mocker.patch.object(settings, "LLM_PIPELINE_ENABLED", False)
    session_factory = mocker.patch("app.celery_worker.SessionLocal")
    logic = mocker.patch("app.celery_worker._generate_reply_logic")
    retry = mocker.patch.object(generate_reply, "retry")

    result = generate_reply("conv-1", "prompt")

    assert result == {"status": "skipped", "reason": "LLM_PIPELINE_ENABLED=false"}
    session_factory.assert_not_called()
    logic.assert_not_called()
    retry.assert_not_called()


def test_generate_reply_disabled_covers_replay_mode(mocker) -> None:
    from app.celery_worker import generate_reply

    mocker.patch.object(settings, "LLM_PIPELINE_ENABLED", False)
    logic = mocker.patch("app.celery_worker._generate_reply_logic")

    result = generate_reply(
        "inc-1", "replay", replay_mode=True, snapshot={"payload": {}}
    )

    assert result["status"] == "skipped"
    logic.assert_not_called()


# ── /copilot при выключенном флаге ───────────────────────────────────────


@pytest.fixture()
def copilot_client(monkeypatch):
    import app.main as main_module
    import app.repository as repository_module
    from app.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(repository_module, "SessionLocal", session_factory)

    fake_task = MagicMock()
    fake_task.delay.return_value = SimpleNamespace(id="task-1")
    monkeypatch.setattr(main_module, "generate_reply", fake_task)

    rate_limit_calls: list[str] = []

    async def _allow(user_id: str) -> bool:
        rate_limit_calls.append(user_id)
        return True

    monkeypatch.setattr(
        main_module, "resilience", SimpleNamespace(check_rate_limit=_allow)
    )

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(main_module, "_remember_job_owner", _noop)
    main_module.app.dependency_overrides[get_current_user] = lambda: User(
        sub="alice", email="alice@example.com", roles=[]
    )
    client = TestClient(main_module.app, raise_server_exceptions=False)
    yield SimpleNamespace(
        client=client,
        task=fake_task,
        sessions=session_factory,
        rate_limit_calls=rate_limit_calls,
    )
    main_module.app.dependency_overrides.clear()
    engine.dispose()


def _conversation_count(session_factory) -> int:
    from app.models import Conversation

    with session_factory() as db:
        return db.query(Conversation).count()


def test_copilot_disabled_answers_503_and_enqueues_nothing(
    copilot_client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", False)

    r = copilot_client.client.post("/copilot", json={"prompt": "почему упал под?"})

    assert r.status_code == 503
    assert "LLM_PIPELINE_ENABLED" in r.json()["detail"]
    assert "Location" not in r.headers
    copilot_client.task.delay.assert_not_called()
    # Ни диалога, ни сообщения, ни списания из rate-limit-бакета.
    assert _conversation_count(copilot_client.sessions) == 0
    assert copilot_client.rate_limit_calls == []


def test_copilot_enabled_still_enqueues(copilot_client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True)

    r = copilot_client.client.post("/copilot", json={"prompt": "почему упал под?"})

    assert r.status_code == 202
    copilot_client.task.delay.assert_called_once()
    assert _conversation_count(copilot_client.sessions) == 1


def test_copilot_disabled_still_requires_auth(monkeypatch) -> None:
    """503 не должен стать подсказкой о конфигурации для анонима."""
    import app.main as main_module

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", False)
    main_module.app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(main_module.app, raise_server_exceptions=False)

    r = client.post("/copilot", json={"prompt": "hi"})

    assert r.status_code in (401, 403)


# ── /replay при выключенном флаге ────────────────────────────────────────


def _replay_app(user: User) -> FastAPI:
    from app.api import replay

    app = FastAPI()
    app.include_router(
        replay.router, prefix="/replay", dependencies=[Depends(get_current_user)]
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def test_replay_disabled_answers_503_and_enqueues_nothing(monkeypatch) -> None:
    from app.api import replay

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", False)
    fake_task = MagicMock()
    monkeypatch.setattr(replay, "generate_reply", fake_task)
    session_factory = MagicMock()
    monkeypatch.setattr(replay, "SessionLocal", session_factory)
    app = _replay_app(User(sub="ops", email="ops@example.com", roles=["approver"]))

    r = TestClient(app, raise_server_exceptions=False).post("/replay/inc-1")

    assert r.status_code == 503
    assert "LLM_PIPELINE_ENABLED" in r.json()["detail"]
    fake_task.delay.assert_not_called()
    session_factory.assert_not_called()


def test_replay_disabled_keeps_role_gate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", False)
    app = _replay_app(User(sub="dev", email="dev@example.com", roles=[]))

    r = TestClient(app, raise_server_exceptions=False).post("/replay/inc-1")

    assert r.status_code == 403
