"""IDOR на `/copilot`: диалог теперь принадлежит пользователю.

До 08.08.2026 `POST /copilot` брал `conversation_id` из тела запроса и не
сверял его ни с чем: у `Conversation` вообще не было владельца. Любой
аутентифицированный пользователь A мог передать UUID диалога пользователя B —
сообщение дописывалось в чужой диалог, `generate_reply` двигал его state
machine, а результат A забирал через `/jobs/{task_id}`. Горизонтальная запись
в чужие данные.

Здесь закреплены все четыре ветки контроля доступа + поведение legacy-строк
(owner_sub IS NULL), у которых владельца нет и взять его неоткуда.

БД: отдельный sqlite in-memory engine, подменяющий `app.repository.SessionLocal`.
Реальный postgres для этих тестов не нужен и на CI-runner'е недоступен
(см. tests/conftest.py::_has_live_postgres).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import User, get_current_user
from app.models import Base, Conversation, Message


# ── фикстуры ─────────────────────────────────────────────────────────────


@pytest.fixture()
def db_sessionmaker():
    """sqlite in-memory на весь тест.

    StaticPool + shared connection: repository гоняет запись через
    run_in_threadpool (другой поток), а обычный sqlite-пул дал бы там СВОЮ
    базу — таблицы «исчезали» бы между запросом и проверкой.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def client(monkeypatch, db_sessionmaker):
    """TestClient над app.main с подменённой БД, celery и rate-limit-ом."""
    import app.main as main_module
    import app.repository as repository_module

    monkeypatch.setattr(repository_module, "SessionLocal", db_sessionmaker)

    # Celery не трогаем: важно только, поставилась задача или нет.
    fake_task = MagicMock()
    fake_task.delay.return_value = SimpleNamespace(id="task-123")
    monkeypatch.setattr(main_module, "generate_reply", fake_task)

    # Per-user rate limit ходит в Redis — подменяем на «всегда разрешено»,
    # чтобы тест не зависел от сети.
    async def _allow(_user_id: str) -> bool:
        return True

    monkeypatch.setattr(
        main_module, "resilience", SimpleNamespace(check_rate_limit=_allow)
    )

    # Без `with`: контекстный менеджер TestClient поднял бы lifespan, а он
    # биндит prometheus-порт 8001 и лезет в БД за contract check. Роуты
    # работают и без него.
    test_client = TestClient(main_module.app, raise_server_exceptions=False)
    test_client.fake_task = fake_task  # type: ignore[attr-defined]
    yield test_client

    main_module.app.dependency_overrides.clear()


def _as_user(client: TestClient, sub: str) -> None:
    """Считать все последующие запросы сделанными от имени `sub`."""
    import app.main as main_module

    main_module.app.dependency_overrides[get_current_user] = lambda: User(
        sub=sub, email=f"{sub}@example.com", roles=[]
    )


def _post(client: TestClient, prompt: str, conversation_id=None):
    body: dict = {"prompt": prompt}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    return client.post("/copilot", json=body)


def _make_conversation(db_sessionmaker, owner_sub) -> uuid.UUID:
    """Кладёт в БД диалог с заданным владельцем (None = legacy-строка)."""
    with db_sessionmaker() as session:
        conv = Conversation(owner_sub=owner_sub)
        session.add(conv)
        session.commit()
        return conv.id


def _enable_adoption(monkeypatch) -> None:
    """Включает COPILOT_LEGACY_CONVERSATION_ADOPTION для app.repository.

    Подменяется весь `settings`-объект: pydantic-Settings не даёт присвоить
    поле, которого нет в модели (extra != "allow"), а этой настройки в
    app/config.py пока нет — repository берёт её через getattr с дефолтом.
    """
    import app.repository as repository_module

    monkeypatch.setattr(
        repository_module,
        "settings",
        SimpleNamespace(COPILOT_LEGACY_CONVERSATION_ADOPTION=True),
    )


def _message_count(db_sessionmaker, conv_id: uuid.UUID) -> int:
    with db_sessionmaker() as session:
        return (
            session.query(Message).filter(Message.conversation_id == conv_id).count()
        )


# ── happy path ───────────────────────────────────────────────────────────


def test_new_conversation_gets_owner(client, db_sessionmaker):
    """Диалог, созданный через /copilot, принадлежит вызвавшему пользователю."""
    _as_user(client, "alice")
    r = _post(client, "почему упал под?")
    assert r.status_code == 202, r.text

    conv_id = uuid.UUID(r.json()["conversation_id"])
    with db_sessionmaker() as session:
        conv = session.get(Conversation, conv_id)
        assert conv is not None
        assert conv.owner_sub == "alice"


def test_own_conversation_accepted(client, db_sessionmaker):
    """Продолжение СВОЕГО диалога работает: сообщение записано, задача поставлена."""
    conv_id = _make_conversation(db_sessionmaker, "alice")
    _as_user(client, "alice")

    r = _post(client, "а теперь логи", conversation_id=conv_id)
    assert r.status_code == 202, r.text
    assert r.json()["conversation_id"] == str(conv_id)
    assert _message_count(db_sessionmaker, conv_id) == 1
    client.fake_task.delay.assert_called_once()


# ── IDOR: чужой и несуществующий диалог ──────────────────────────────────


def test_foreign_conversation_rejected_with_404(client, db_sessionmaker):
    """Диалог bob'а, запрос от alice → 404, БЕЗ записи и БЕЗ задачи.

    Именно 404, а не 403: 403 подтвердил бы существование чужого диалога и
    сделал бы ручку оракулом для перебора UUID-ов.
    """
    conv_id = _make_conversation(db_sessionmaker, "bob")
    _as_user(client, "alice")

    r = _post(client, "покажи чужую переписку", conversation_id=conv_id)
    assert r.status_code == 404
    # Чужой диалог не тронут: ни сообщения, ни сдвига state machine.
    assert _message_count(db_sessionmaker, conv_id) == 0
    client.fake_task.delay.assert_not_called()


def test_missing_conversation_rejected_with_404(client, db_sessionmaker):
    """Несуществующий UUID → тот же 404, что и чужой (ответы неразличимы)."""
    _as_user(client, "alice")

    r = _post(client, "hello", conversation_id=uuid.uuid4())
    assert r.status_code == 404
    client.fake_task.delay.assert_not_called()


def test_foreign_and_missing_responses_are_indistinguishable(
    client, db_sessionmaker
):
    """Ответ на чужой и на несуществующий диалог совпадает побайтово."""
    foreign_id = _make_conversation(db_sessionmaker, "bob")
    _as_user(client, "alice")

    foreign = _post(client, "x", conversation_id=foreign_id)
    missing = _post(client, "x", conversation_id=uuid.uuid4())

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_owner_cannot_be_spoofed_via_body(client, db_sessionmaker):
    """owner_sub берётся из JWT, а не из тела — лишнее поле игнорируется."""
    _as_user(client, "alice")
    r = client.post("/copilot", json={"prompt": "hi", "owner_sub": "bob"})
    assert r.status_code == 202, r.text

    conv_id = uuid.UUID(r.json()["conversation_id"])
    with db_sessionmaker() as session:
        assert session.get(Conversation, conv_id).owner_sub == "alice"


# ── legacy-строки (owner_sub IS NULL) ────────────────────────────────────


def test_legacy_conversation_without_owner_is_foreign_by_default(
    client, db_sessionmaker
):
    """Строка без владельца по умолчанию = ЧУЖАЯ (404).

    Осознанный консервативный выбор: до миграции 20260808_0100 владельца не
    писал никто, поэтому NULL значит «неизвестно чей», а не «ничей». Отдать
    такую строку первому постучавшему = оставить IDOR открытым ещё на один
    заход. Цена — настоящий владелец теряет доступ к своей истории; лечится
    новым диалогом или ручным backfill-ом оператора.
    """
    conv_id = _make_conversation(db_sessionmaker, None)
    _as_user(client, "alice")

    r = _post(client, "продолжаю старый диалог", conversation_id=conv_id)
    assert r.status_code == 404
    assert _message_count(db_sessionmaker, conv_id) == 0
    client.fake_task.delay.assert_not_called()

    with db_sessionmaker() as session:
        # Владелец НЕ проставился — отказ не имеет побочных эффектов.
        assert session.get(Conversation, conv_id).owner_sub is None


def test_legacy_conversation_adopted_when_flag_enabled(
    client, db_sessionmaker, monkeypatch
):
    """Аварийный вентиль COPILOT_LEGACY_CONVERSATION_ADOPTION=True усыновляет.

    Настройки нет в app/config.py (другой владелец файла) — repository читает
    её через getattr с дефолтом False. Тест закрепляет обе стороны: и что
    флаг работает, и что по умолчанию он выключен (тест выше).

    Подменяем весь объект settings, а не поле: pydantic-Settings запрещает
    присваивание неизвестных полей (extra != "allow"), так что до появления
    поля в app/config.py setattr на нём невозможен.
    """
    _enable_adoption(monkeypatch)

    conv_id = _make_conversation(db_sessionmaker, None)
    _as_user(client, "alice")

    r = _post(client, "продолжаю старый диалог", conversation_id=conv_id)
    assert r.status_code == 202, r.text
    with db_sessionmaker() as session:
        assert session.get(Conversation, conv_id).owner_sub == "alice"


def test_adoption_flag_does_not_open_foreign_conversations(
    client, db_sessionmaker, monkeypatch
):
    """Вентиль усыновления НЕ трогает диалоги с ЖИВЫМ чужим владельцем."""
    _enable_adoption(monkeypatch)

    conv_id = _make_conversation(db_sessionmaker, "bob")
    _as_user(client, "alice")

    r = _post(client, "x", conversation_id=conv_id)
    assert r.status_code == 404
    with db_sessionmaker() as session:
        assert session.get(Conversation, conv_id).owner_sub == "bob"


# ── repository: фильтрация по владельцу на чтении ────────────────────────


@pytest.mark.asyncio
async def test_repository_read_paths_filter_by_owner(monkeypatch, db_sessionmaker):
    """get_conversation / list_conversations тоже отсекают чужое."""
    import app.repository as repository_module

    monkeypatch.setattr(repository_module, "SessionLocal", db_sessionmaker)

    alice_id = _make_conversation(db_sessionmaker, "alice")
    bob_id = _make_conversation(db_sessionmaker, "bob")
    legacy_id = _make_conversation(db_sessionmaker, None)

    from fastapi import HTTPException

    conv = await repository_module.get_conversation(alice_id, owner_sub="alice")
    assert conv.id == alice_id

    for foreign_id in (bob_id, legacy_id):
        with pytest.raises(HTTPException) as exc:
            await repository_module.get_conversation(foreign_id, owner_sub="alice")
        assert exc.value.status_code == 404

    listed = await repository_module.list_conversations(owner_sub="alice")
    assert [c.id for c in listed] == [alice_id]

    # Без owner_sub поведение прежнее (внутренние/админские вызовы).
    assert len(await repository_module.list_conversations()) == 3


# ── миграция 20260808_0100 ───────────────────────────────────────────────


def test_migration_upgrade_downgrade_roundtrip_on_sqlite():
    """upgrade/downgrade миграции отрабатывают на sqlite и идемпотентны.

    Гоняем модуль миграции напрямую через Operations.context — так тест не
    зависит от остальной цепочки ревизий (её параллельно правят).
    """
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Column, DateTime, MetaData, String, Table

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260808_0100_add_conversation_owner.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_20260808_0100", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.revision == "20260808_0100"
    assert module.down_revision == "20260807_0400"

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Таблица в ДОмиграционном виде — без owner_sub.
    meta = MetaData()
    Table(
        "conversations",
        meta,
        Column("id", String(36), primary_key=True),
        Column("current_state", String()),
        Column("created_at", DateTime()),
    )
    meta.create_all(engine)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            cols = {c["name"] for c in sa_inspect(conn).get_columns("conversations")}
            idx = {i["name"] for i in sa_inspect(conn).get_indexes("conversations")}
            assert "owner_sub" in cols
            assert "ix_conversations_owner_sub" in idx

            # Идемпотентность: повторный upgrade не падает.
            module.upgrade()

            module.downgrade()
            cols = {c["name"] for c in sa_inspect(conn).get_columns("conversations")}
            idx = {i["name"] for i in sa_inspect(conn).get_indexes("conversations")}
            assert "owner_sub" not in cols
            assert "ix_conversations_owner_sub" not in idx

            # Повторный downgrade — тоже no-op, а не падение.
            module.downgrade()

    engine.dispose()
