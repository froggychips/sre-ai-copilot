"""Пул и таймауты engine берутся из Settings, а не из хардкода.

DB_POOL_SIZE/DB_MAX_OVERFLOW были объявлены в app/config.py («сайзить api и
worker раздельно»), но create_engine хардкодил 10/20 — env-переменные молча
игнорировались. Плюс connect_args не задавал connect_timeout: зависший PG
(fsync-recovery) держал новый коннект до TCP-таймаута ОС — усилитель
инцидента 08.08.2026, когда /readyz ждал коннект и kubelet убивал под.

Здесь же — guard на ИНВАРИАНТ ВРЕМЕНИ (см. docstring app/database.py и
docs/SEMANTIC_CONTRACT.md §10): в БД лежит naive UTC, а сессия принудительно
в UTC. Проверяемо и дёшево, в отличие от массовой замены 67 × `utcnow()`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import DateTime, create_engine

import app.database as db_mod

_FAKE_CFG = SimpleNamespace(DB_POOL_SIZE=7, DB_MAX_OVERFLOW=13, DB_CONNECT_TIMEOUT=3)
_PG_URL = "postgresql+psycopg2://user:pass@localhost:5432/testdb"


def test_pool_kwargs_come_from_settings_object():
    """pool_size/max_overflow/connect_timeout читаются из переданного cfg."""
    kwargs = db_mod._build_pool_kwargs(_PG_URL, cfg=_FAKE_CFG)
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 13
    assert kwargs["connect_args"]["connect_timeout"] == 3
    # Инвариант инцидента 08.08 не потерян при рефакторинге:
    assert "idle_in_transaction_session_timeout" in kwargs["connect_args"]["options"]


def test_pool_kwargs_default_cfg_is_app_settings():
    """Без явного cfg значения приходят из app.config.settings (реальный wiring)."""
    from app.config import settings

    kwargs = db_mod._build_pool_kwargs(_PG_URL)
    assert kwargs["pool_size"] == settings.DB_POOL_SIZE
    assert kwargs["max_overflow"] == settings.DB_MAX_OVERFLOW
    assert kwargs["connect_args"]["connect_timeout"] == settings.DB_CONNECT_TIMEOUT


def test_sqlite_gets_no_pool_kwargs():
    """sqlite (тесты) — без QueuePool-параметров: TypeError/бессмысленно."""
    assert db_mod._build_pool_kwargs("sqlite:///./x.db", cfg=_FAKE_CFG) == {}


def test_create_engine_receives_settings_values():
    """create_engine с нашими kwargs реально конфигурирует QueuePool.

    Engine не коннектится при создании — проверяем без живого Postgres.
    """
    engine = create_engine(
        _PG_URL,
        echo=False,
        pool_pre_ping=True,
        **db_mod._build_pool_kwargs(_PG_URL, cfg=_FAKE_CFG),
    )
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 13
    finally:
        engine.dispose()


def test_module_engine_kwargs_match_builder():
    """Модульный engine собран ровно из _build_pool_kwargs(settings.DATABASE_URL)."""
    from app.config import settings

    assert db_mod._pool_kwargs == db_mod._build_pool_kwargs(settings.DATABASE_URL)


# ── Инвариант времени: сессия PG принудительно в UTC ──────────────────────


def test_pg_session_timezone_pinned_to_utc():
    """connect_args пришпиливает TimeZone сессии к UTC.

    Несущий инвариант, а не косметика. В колонках лежит naive UTC (все
    DateTime — TIMESTAMP WITHOUT TIME ZONE, python-default `utcnow()`), а
    десятки raw-SQL окон сравнивают их с `NOW()` (timestamptz):
    `WHERE ts > NOW() - INTERVAL '24 hours'` в stats_digest,
    `resolved_at=NOW()` в alerts_resolve_sync. PG приводит timestamptz к
    timestamp ПО ТАЙМЗОНЕ СЕССИИ, а её дефолт приходит извне приложения
    (postgresql.conf / ALTER ROLE / PGTZ пода). На инстансе с не-UTC зоной
    все окна съедут на offset МОЛЧА: дайджест покажет не те сутки, алерты
    недорезолвятся — ни ошибки, ни алерта.
    """
    options = db_mod._build_pool_kwargs(_PG_URL, cfg=_FAKE_CFG)["connect_args"]["options"]
    assert "-c timezone=UTC" in options, (
        f"таймзона сессии не пришпилена, options={options!r}"
    )


def test_options_keep_both_settings_parseable():
    """Оба `-c`-параметра живут в одной строке options и не съедают друг друга."""
    options = db_mod._build_pool_kwargs(_PG_URL, cfg=_FAKE_CFG)["connect_args"]["options"]
    flags = dict(
        part.split("=", 1) for part in options.replace("-c ", "").split() if "=" in part
    )
    assert flags.get("timezone") == "UTC"
    assert int(flags["idle_in_transaction_session_timeout"]) > 0


def test_sqlite_path_untouched_by_timezone_option():
    """sqlite-ветка (тесты) options не получает вовсе — libpq-опций там нет."""
    assert db_mod._build_pool_kwargs("sqlite:///./x.db", cfg=_FAKE_CFG) == {}


def test_model_datetime_defaults_are_naive_utc():
    """python-side default'ы дают NAIVE datetime со значением в UTC.

    Именно этот контракт делает TIMESTAMP WITHOUT TIME ZONE корректным.
    Если чей-то дефолт станет aware, запись в колонку без зоны отбросит
    tzinfo (сдвиг на offset), а сравнение с naive-значениями в Python
    начнёт бросать TypeError.
    """
    from app.models import Conversation, Message

    columns = [
        db_mod.IncidentRecord.__table__.c.created_at,
        Conversation.__table__.c.created_at,
        Conversation.__table__.c.updated_at,
        Message.__table__.c.created_at,
    ]
    def _utc_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    before = _utc_naive()
    for column in columns:
        assert column.default is not None, f"{column} без default"
        value = column.default.arg({})
        assert value.tzinfo is None, f"{column}: default стал aware — {value!r}"
        # Значение именно UTC, а не локальное время машины: сравниваем с
        # эталонным aware-now, приведённым к naive.
        assert abs((value - _utc_naive()).total_seconds()) < 60, (
            f"{column}: default не в UTC — {value!r}"
        )
        assert value >= before.replace(microsecond=0)


def test_datetime_columns_are_without_timezone():
    """Ни одна DateTime-колонка не объявлена timezone=True.

    Смешивать в одной схеме timestamptz и timestamp — это ровно тот дрейф,
    из-за которого окна начинают считаться по разным зонам.
    """
    import app.knowledge_graph.schema  # noqa: F401 — регистрация kg_* в metadata
    import app.remediation.models  # noqa: F401
    import app.services.discord.dedup_store  # noqa: F401
    from app.models import Base as ModelsBase

    aware = [
        f"{table.name}.{column.name}"
        for metadata in (ModelsBase.metadata, db_mod.Base.metadata)
        for table in metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and column.type.timezone
    ]
    assert not aware, f"колонки с timezone=True при naive-UTC контракте: {aware}"
