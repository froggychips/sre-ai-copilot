"""Схема БД совпадает с той, которую ожидает выкаченный код.

Прецедент 14.08.2026: образ rc.19 нёс код с колонкой `owner_source`, а
миграции в проде не применили — рецепт релиза их просто не содержал.
`kg_topology_sync` падал на ВСЕХ 129 namespace с
`column "owner_source" does not exist` и за тик писал ноль узлов, ноль рёбер.

Заметили это не по алерту, а по косвенному признаку: тик подозрительно
быстро завершился (40 секунд вместо минут). Данные уцелели только потому,
что deadman у `edge_decay` увидел fetch-ошибки и отказался удалять 17 тысяч
рёбер — то есть по счастливой особенности архитектуры, а не потому, что
кто-то следил за версией схемы.
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.self_health import (_ALL_CHECKS, _expected_head_revision,
                                             _known_revisions,
                                             check_schema_version)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.execute(text("CREATE TABLE alembic_version (version_num VARCHAR PRIMARY KEY)"))
    s.commit()
    return s


def _set_revision(db, rev: str) -> None:
    db.execute(text("DELETE FROM alembic_version"))
    db.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": rev})
    db.commit()


# --- разбор ревизий -------------------------------------------------------


def test_head_revision_is_single_and_known():
    """У цепочки миграций ровно одна голова — иначе alembic сам сломается."""
    head = _expected_head_revision()
    assert head is not None, "не удалось определить head — ветвление в миграциях?"
    assert head in _known_revisions()


def test_revisions_are_discovered():
    assert len(_known_revisions()) > 10, "ревизии не читаются из alembic/versions"


# --- поведение проверки ---------------------------------------------------


def test_matching_revision_is_ok(db):
    _set_revision(db, _expected_head_revision())
    result = check_schema_version(db)
    assert result.status == "ok"
    assert result.detail["db_revision"] == result.detail["code_head"]


def test_outdated_schema_is_a_failure(db):
    """Ровно случай 14.08: код ждёт колонку, которой в БД нет."""
    revisions = _known_revisions()
    _set_revision(db, revisions[0])          # самая старая ревизия

    result = check_schema_version(db)
    assert result.status == "fail"
    assert "отстаёт" in result.detail["direction"]


def test_unknown_future_revision_is_only_a_warning(db):
    """БД новее кода — бывает при откате образа и само по себе не ломает."""
    _set_revision(db, "99999999_9999")

    result = check_schema_version(db)
    assert result.status in ("warn", "fail")
    assert result.detail["db_revision"] == "99999999_9999"


def test_missing_alembic_table_is_a_warning_not_crash():
    """В тестовой БД таблицы может не быть — это не повод падать."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    result = check_schema_version(s)
    assert result.status == "warn"
    assert "alembic_version" in result.detail["reason"]


def test_check_is_registered():
    """Проверка, не включённая в набор, не выполняется никогда."""
    assert check_schema_version in _ALL_CHECKS
