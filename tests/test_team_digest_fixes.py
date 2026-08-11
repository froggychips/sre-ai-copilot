"""Регрессионные тесты для review-fixes в team_digest.

Покрытие:
  * node_kind — счётчики команды считают ТОЛЬКО node_kind='service'
    (contract 2.4). Без фильтра workload-узлы (Deployment'ы) лежат в той же
    таблице kg_services и попадают в выдачу: «Services: N» выходил примерно
    вдвое больше, чем у stats digest, а один сервис занимал в Fragile top-5
    две строки (service + его workload).
  * ReadOnlyAutocommitSession — обход всех команд перемежает SQL с Discord I/O
    (ретраи до ~40с на команду). Обычная сессия висела бы всё это время
    `idle in transaction`, а PG рвёт такие соединения через 120с — тот же
    антипаттерн, который #252 чинил в stats/chronic. Дайджест по PG read-only.

SQL-часть проверяется против живого Postgres (по образцу
test_kg_quality_counts_only_service_nodes в test_digest_sections_against_schema);
там где PG нет, фильтр всё равно проверяется по скомпилированному запросу.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import requires_postgres


# ── node_kind: фильтр присутствует в самих запросах ─────────────────────────


class _FilterRecorder:
    """Session-заглушка, записывающая ORM-критерии `.filter(...)`.

    Живой Postgres для проверки самого факта фильтра не нужен: достаточно
    увидеть, что node_kind-условие доехало до запроса.
    """

    def __init__(self) -> None:
        self.criteria: list = []

    def query(self, *entities):
        return self

    def filter(self, *criteria):
        self.criteria.extend(criteria)
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _n):
        return self

    def all(self):
        return []

    def scalar(self):
        return 0


def _criteria_text(rec: _FilterRecorder) -> str:
    return " ".join(str(c) for c in rec.criteria)


def test_service_count_query_filters_node_kind():
    """`_real_service_count` фильтрует node_kind — иначе workload-узлы
    удваивают «Services: N» относительно stats digest."""
    from app.services import team_digest

    rec = _FilterRecorder()
    assert team_digest._real_service_count(rec, "kingdom1") == 0

    sql = _criteria_text(rec)
    assert "node_kind" in sql, "фильтр node_kind потерян — workload-узлы снова считаются"
    assert "synthetic" in sql
    assert "team_owner" in sql


def test_fragile_query_filters_node_kind():
    """Тот же контракт для Fragile top-5: иначе один сервис даёт две строки."""
    from app.services import team_digest

    rec = _FilterRecorder()
    assert team_digest._top_fragile_services(rec, "kingdom1") == []

    sql = _criteria_text(rec)
    assert "node_kind" in sql, "Fragile top-5 снова смешивает service и workload"


# ── node_kind против живой схемы ────────────────────────────────────────────


@pytest.fixture(scope="module")
def db_session():
    import app.knowledge_graph.schema  # noqa: F401
    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    yield session
    session.close()


@requires_postgres
def test_team_counters_ignore_workload_nodes(db_session):
    """workload-узел не должен ни увеличивать «Services», ни попадать в Fragile.

    Регрессия: узлы обоих типов живут в kg_services, и запрос без node_kind
    считает их вместе — «Services: N» у команды расходился со stats digest
    примерно вдвое, а в Fragile top-5 один сервис занимал две строки.
    """
    from app.knowledge_graph.schema import (NODE_KIND_SERVICE,
                                            NODE_KIND_WORKLOAD, Service)
    from app.services import team_digest

    team = "__nodekind_team__"
    ns = "__nodekind_team_ns__"
    db_session.query(Service).filter_by(namespace=ns).delete()
    db_session.commit()

    before = team_digest._real_service_count(db_session, team)

    svc = Service(name="probe", namespace=ns, team_owner=team,
                  node_kind=NODE_KIND_SERVICE, synthetic=False,
                  health_score=0.1)
    workload = Service(name="probe", namespace=ns, team_owner=team,
                       node_kind=NODE_KIND_WORKLOAD, synthetic=False,
                       health_score=0.1)
    db_session.add_all([svc, workload])
    db_session.commit()
    try:
        after = team_digest._real_service_count(db_session, team)
        assert after == before + 1, (
            f"workload-узел посчитан как сервис: {before} → {after}"
        )
        fragile = team_digest._top_fragile_services(db_session, team)
        assert len(fragile) == 1, (
            f"один сервис попал в Fragile двумя строками: {fragile}"
        )
    finally:
        db_session.query(Service).filter_by(namespace=ns).delete()
        db_session.commit()


# ── Транзакция не живёт через Discord I/O ───────────────────────────────────


def _fake_digest(team: str) -> dict:
    return {
        "team_owner": team,
        "window_hours": 24,
        "generated_at": "2026-08-10T00:00:00+00:00",
        "service_count": 1,
        "fragile": [],
        "deploys": {"total": 0, "success": 0, "failed": 0, "success_pct": None},
        "alerts": {"open_total": 0, "by_severity": {}, "top_alertnames": []},
        "slo": None,
        "stuck": [],
    }


@pytest.mark.asyncio
async def test_send_team_digest_uses_readonly_autocommit_session():
    """Собственная сессия send_team_digest — AUTOCOMMIT read-only, и она
    закрывается ДО Discord-отправки (не висит idle in transaction)."""
    from app.services import team_digest

    fake_db = MagicMock()
    factory = MagicMock(return_value=fake_db)
    with patch.object(team_digest.settings, "TEAM_DIGEST_ENABLED", True, create=True), \
         patch.object(team_digest.settings, "DISCORD_DRY_RUN", True), \
         patch.object(team_digest, "ReadOnlyAutocommitSession", factory), \
         patch.object(team_digest, "build_team_digest",
                      return_value=_fake_digest("kingdom1")):
        result = await team_digest.send_team_digest("kingdom1")

    assert result["status"] == "dry_run"
    factory.assert_called_once()
    fake_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_send_all_team_digests_uses_readonly_autocommit_session():
    """Per-team сессии в batch-рассылке — тоже AUTOCOMMIT: 60+ команд подряд
    с ретраями Discord держали обычную транзакцию открытой минутами."""
    from app.services import team_digest

    sessions = []

    def _factory():
        db = MagicMock()
        sessions.append(db)
        return db

    factory = MagicMock(side_effect=_factory)
    with patch.object(team_digest.settings, "TEAM_DIGEST_ENABLED", True, create=True), \
         patch.object(team_digest, "ReadOnlyAutocommitSession", factory), \
         patch.object(team_digest, "_distinct_team_owners",
                      return_value=["kingdom1", "kingdom2"]), \
         patch.object(team_digest, "find_stuck_alerts", return_value=[]), \
         patch.object(team_digest, "send_team_digest",
                      new=AsyncMock(return_value={"status": "sent"})):
        stats = await team_digest.send_all_team_digests()

    assert stats["teams_total"] == 2
    assert stats["sent"] == 2
    # Одна сессия на prefetch + по одной на команду, все закрыты.
    assert factory.call_count == 3
    for db in sessions:
        db.close.assert_called_once()


def test_team_digest_does_not_use_read_write_session():
    """Модуль не должен тащить обычный SessionLocal обратно: любая запись
    через AUTOCOMMIT-сессию нелегальна, а read-write сессия вернула бы
    idle-in-transaction (#252)."""
    import pathlib

    src = pathlib.Path("app/services/team_digest.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in src, (
        "team_digest снова открывает read-write сессию через Discord I/O"
    )
