"""Индексы, которые обещает модель, должны существовать в базе.

Замер прода 19.08.2026: на таблице `incidents` не было **ни одного** индекса
— при том что модель объявляет у `incident_id` и `unique=True`, и
`index=True`. То есть уникальность держалась на честном слове приложения:
две записи с одним `incident_id` СУБД бы приняла.

Отдельно колонки были типа `json`, а не `jsonb`. У `json` в PostgreSQL нет
ни GIN-индексов, ни операторов `?`/`@>`: значение хранится текстом и
разбирается заново при каждом обращении. Между тем в `analysis` живёт машина
состояний из девяти ключей, включая claim исполнителя.

Оба факта не видны из кода — только из базы. Поэтому тест смотрит на
миграцию: она единственное место, где это можно проверить без живого
PostgreSQL.
"""
import pathlib
import re

import pytest

MIGRATION = (pathlib.Path(__file__).parent.parent / "alembic" / "versions"
             / "20260819_0100_incidents_jsonb_indexes.py")


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def module():
    """Сама миграция: SQL там собирается циклом, а не литералами."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("m_incidents_idx", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_exists():
    assert MIGRATION.is_file(), "миграция про индексы incidents пропала"


@pytest.mark.parametrize("column", ["data", "analysis", "trace", "user_feedback"])
def test_every_json_column_becomes_jsonb(module, sql, column):
    """`json` нельзя проиндексировать — значит нельзя и спросить."""
    assert column in module._JSON_COLUMNS, (
        f"колонка {column} не переводится в jsonb: GIN по ней невозможен"
    )
    assert "TYPE jsonb" in sql


def test_incident_id_gets_a_unique_index(sql):
    """Модель обещает unique — база обязана это обеспечивать."""
    assert "CREATE UNIQUE INDEX" in sql and "incident_id" in sql


def test_lookup_columns_are_indexed(sql):
    """status и created_at — то, по чему инциденты ищут глазами и кодом."""
    for column in ("status", "created_at"):
        # SQL собирается f-строками с переносами, поэтому ищем по имени
        # индекса и по колонке отдельно, а не одним выражением.
        assert f"ix_incidents_{column}" in sql, (
            f"нет индекса по {column} — разбор инцидентов идёт полным сканом"
        )
        assert f"({column}" in sql


def test_analysis_gets_a_gin_index(sql):
    """Состояния живут внутри analysis; без GIN поиск по ним — скан."""
    assert re.search(r"USING GIN \(analysis", sql)


def test_migration_sets_lock_timeout(sql):
    """ALTER TYPE берёт ACCESS EXCLUSIVE — висящий читатель не должен утащить."""
    assert "lock_timeout" in sql


def test_downgrade_removes_what_upgrade_added(sql):
    """Обратимость: индексы снимаются, тип возвращается."""
    down = sql[sql.index("def downgrade"):]
    assert "DROP INDEX" in down
    assert "TYPE json" in down


def test_sqlite_is_skipped(sql):
    """Тесты гоняются на sqlite, где JSON — один тип и ALTER не нужен."""
    assert 'if _dialect() != "postgresql"' in sql


# --- модель не должна снова разъехаться с базой ---------------------------


def test_model_documents_the_state_machine():
    """Девять ключей состояния в JSON — это должно быть написано в модели.

    Иначе следующий человек увидит «поле с результатом разбора» и не узнает,
    что там же лежит claim исполнителя.
    """
    from app.database import IncidentRecord
    doc = (IncidentRecord.__doc__ or "").lower()
    assert "состояни" in doc and "claim" in doc, (
        "модель не объясняет, что analysis — ещё и машина состояний"
    )
