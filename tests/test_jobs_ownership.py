"""IDOR на `GET /jobs/{task_id}`: результат задачи принадлежит поставившему её.

До фикса ручка отдавала РЕЗУЛЬТАТ любой celery-задачи любому
аутентифицированному пользователю: ownership-проверки не было вовсе, а
защитой считался «недогадываемый» task_id. Но task_id светится в
Location-заголовке и теле ответа /copilot, в access-логах ingress-а и в
логах приложения — то есть это не секрет, а идентификатор.

Здесь закреплено:
  * /copilot ЗАПИСЫВАЕТ владельца (job_owner:<task_id> → JWT sub) с TTL;
  * свой task_id читается;
  * чужой task_id → 404 (анти-оракул: неотличимо от несуществующего);
  * нет записи о владельце (Redis лёг / ключ протух / задачу поставил другой
    код-путь) → тоже 404, fail-closed;
  * ошибка Redis на записи владельца НЕ роняет постановку задачи (202), но
    делает результат нечитаемым — осознанная цена fail-closed.

Redis подменяется in-memory двойником: живой Redis для этих тестов не нужен и
на CI-runner'е недоступен.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import User, get_current_user
from app.models import Base


class FakeRedis:
    """Минимальный async-двойник: set(nx, ex) / get + режим «Redis лёг»."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.fail = False

    async def set(self, key, value, ex=None, nx=False):
        if self.fail:
            raise ConnectionError("redis unavailable")
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.store.get(key)


@pytest.fixture()
def db_sessionmaker():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def fake_redis():
    return FakeRedis()


@pytest.fixture()
def client(monkeypatch, db_sessionmaker, fake_redis):
    """TestClient над app.main с подменёнными БД, celery, redis и rate-limit-ом."""
    import app.main as main_module
    import app.repository as repository_module

    monkeypatch.setattr(repository_module, "SessionLocal", db_sessionmaker)
    monkeypatch.setattr(main_module, "redis_client", fake_redis)

    fake_task = MagicMock()
    counter = {"n": 0}

    def _delay(*args, **kwargs):
        counter["n"] += 1
        return SimpleNamespace(id=f"task-{counter['n']}")

    fake_task.delay.side_effect = _delay
    monkeypatch.setattr(main_module, "generate_reply", fake_task)

    async def _allow(_user_id: str) -> bool:
        return True

    monkeypatch.setattr(
        main_module, "resilience", SimpleNamespace(check_rate_limit=_allow)
    )

    # Celery-result читается через AsyncResult в threadpool — подменяем на
    # предсказуемый снапшот, живой broker/backend не нужен.
    monkeypatch.setattr(
        main_module,
        "_job_status_snapshot",
        lambda task_id: {"task_id": task_id, "status": "SUCCESS", "result": "разбор"},
    )

    test_client = TestClient(main_module.app, raise_server_exceptions=False)
    yield test_client
    main_module.app.dependency_overrides.clear()


def _as_user(sub: str) -> None:
    import app.main as main_module

    main_module.app.dependency_overrides[get_current_user] = lambda: User(
        sub=sub, email=f"{sub}@example.com", roles=[]
    )


def _post_copilot(client: TestClient, prompt: str = "почему упал под?"):
    return client.post("/copilot", json={"prompt": prompt})


# ── happy path ───────────────────────────────────────────────────────────


def test_copilot_records_job_owner(client, fake_redis):
    """Постановка задачи пишет владельца с TTL (NX, чтобы не перепривязать)."""
    _as_user("alice")
    r = _post_copilot(client)
    assert r.status_code == 202, r.text

    task_id = r.json()["task_id"]
    assert fake_redis.store[f"job_owner:{task_id}"] == "alice"
    # TTL обязателен: без него запись переживает результат и копится в Redis.
    assert fake_redis.expirations[f"job_owner:{task_id}"] == 24 * 3600


def test_owner_reads_own_job(client):
    """Свою задачу владелец читает вместе с результатом."""
    _as_user("alice")
    task_id = _post_copilot(client).json()["task_id"]

    r = client.get(f"/jobs/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "разбор"


# ── IDOR ─────────────────────────────────────────────────────────────────


def test_foreign_job_rejected_with_404(client):
    """Задача bob-а, запрос от alice → 404 и БЕЗ результата в теле."""
    _as_user("bob")
    task_id = _post_copilot(client, "чужой разбор").json()["task_id"]

    _as_user("alice")
    r = client.get(f"/jobs/{task_id}")
    assert r.status_code == 404
    assert "result" not in r.json()
    assert "разбор" not in r.text


def test_foreign_and_unknown_jobs_are_indistinguishable(client):
    """404 на чужую и на несуществующую задачу совпадают побайтово (анти-оракул)."""
    _as_user("bob")
    foreign_id = _post_copilot(client).json()["task_id"]

    _as_user("alice")
    foreign = client.get(f"/jobs/{foreign_id}")
    unknown = client.get("/jobs/00000000-0000-0000-0000-000000000000")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


def test_job_without_owner_record_rejected(client, fake_redis):
    """Нет записи о владельце → 404 (fail-closed), даже для своей же задачи."""
    _as_user("alice")
    task_id = _post_copilot(client).json()["task_id"]
    fake_redis.store.clear()  # эмулируем протухший ключ / потерю записи

    assert client.get(f"/jobs/{task_id}").status_code == 404


def test_redis_down_on_read_is_fail_closed(client, fake_redis):
    """Ошибка Redis на чтении владельца — отказ, а не «пропускаем»."""
    _as_user("alice")
    task_id = _post_copilot(client).json()["task_id"]

    fake_redis.fail = True
    assert client.get(f"/jobs/{task_id}").status_code == 404


def test_redis_down_on_write_does_not_break_enqueue(client, fake_redis):
    """Задача ставится даже если владельца записать не удалось.

    Цена fail-closed: результат такой задачи через /jobs не прочитать. Это
    лучше, чем 500 на постановку (работа всё равно выполнится воркером).
    """
    fake_redis.fail = True
    _as_user("alice")
    r = _post_copilot(client)
    assert r.status_code == 202, r.text

    fake_redis.fail = False
    assert client.get(f"/jobs/{r.json()['task_id']}").status_code == 404


def test_owner_record_is_not_rebindable(client, fake_redis):
    """Повторная запись владельца (NX) не перепривязывает задачу на другого."""
    import asyncio

    import app.main as main_module

    _as_user("alice")
    task_id = _post_copilot(client).json()["task_id"]

    asyncio.run(main_module._remember_job_owner(task_id, "attacker"))
    assert fake_redis.store[f"job_owner:{task_id}"] == "alice"


def test_bytes_owner_value_is_compared_correctly(client, fake_redis):
    """Клиент без decode_responses отдаёт bytes — сверка обязана это терпеть."""
    _as_user("alice")
    task_id = _post_copilot(client).json()["task_id"]
    fake_redis.store[f"job_owner:{task_id}"] = b"alice"  # type: ignore[assignment]

    assert client.get(f"/jobs/{task_id}").status_code == 200


def test_anonymous_request_rejected(client):
    """Без токена — 403 от HTTPBearer, ownership тут даже не при чём."""
    import app.main as main_module

    main_module.app.dependency_overrides.clear()
    r = client.get("/jobs/task-1")
    assert r.status_code in (401, 403)
