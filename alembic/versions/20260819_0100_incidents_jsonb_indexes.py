"""incidents: json → jsonb и недостающие индексы

Revision ID: 20260819_0100
Revises: 20260815_0100
Create Date: 2026-08-19 01:00:00.000000

Две находки при разборе замечания «JSONB используется как coordination/state
database» (ревью 19.08.2026). Оказалось, что до JSONB там ещё не дошло.

**1. Тип был `json`, а не `jsonb`.**

`incidents.analysis` держит машину состояний из девяти ключей:
`executor_applied`, `executor_in_flight`, `executor_state_unknown`,
`executor_intent`, `executor_result`, `executor_disabled`, `report_pending`,
`report_sent`, `report_failed`. Среди них `executor_in_flight` — claim,
то есть координационный примитив.

У типа `json` в PostgreSQL нет ни GIN-индексов, ни операторов `?`/`@>`:
значение хранится текстом и разбирается заново при каждом обращении. Любой
вопрос «покажи инциденты с недоставленным отчётом» — полный скан с разбором
JSON на каждой строке.

**2. Индексов на таблице не было ВООБЩЕ.**

Ни одного, включая `incident_id` — при том что модель объявляет
`unique=True, index=True`. То есть уникальность держалась на честном слове
приложения: две записи с одним `incident_id` СУБД бы приняла.

Момент выбран намеренно: таблица сейчас пуста (пайплайн включается флагом
`LLM_PIPELINE_ENABLED`), поэтому `ALTER TYPE` мгновенный и без переписывания
строк. На наполненной таблице это уже была бы долгая блокировка.

Что НЕ делает эта миграция: не выносит состояния в отдельные колонки. Это
следующий шаг и отдельное решение — здесь только фундамент, на котором такой
шаг возможен.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260819_0100"
down_revision = "20260815_0100"
branch_labels = None
depends_on = None

_TABLE = "incidents"
_JSON_COLUMNS = ("data", "analysis", "trace", "user_feedback")
_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def _set_lock_timeout() -> None:
    if _dialect() != "postgresql":
        return
    # ALTER TYPE берёт ACCESS EXCLUSIVE. На пустой таблице это доли секунды,
    # но висящий читатель не должен утащить миграцию за собой
    # (POSTMORTEM 2026-08-08 §3.2).
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def upgrade() -> None:
    if _dialect() != "postgresql":
        return          # sqlite в тестах: JSON и так один тип
    _set_lock_timeout()

    for col in _JSON_COLUMNS:
        op.execute(sa.text(
            f"ALTER TABLE {_TABLE} ALTER COLUMN {col} "
            f"TYPE jsonb USING {col}::jsonb"
        ))

    # Уникальность incident_id: модель её обещает, БД — нет.
    op.execute(sa.text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_incident_id "
        f"ON {_TABLE} (incident_id)"
    ))
    # Разбор инцидентов идёт по статусу и по времени: и то и другое сейчас
    # полный скан.
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS ix_incidents_status ON {_TABLE} (status)"
    ))
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS ix_incidents_created_at "
        f"ON {_TABLE} (created_at DESC)"
    ))
    # GIN по analysis — ради вопросов вида «у кого висит report_pending».
    # jsonb_path_ops компактнее общего варианта и покрывает `@>`, которого
    # для проверки наличия ключа достаточно.
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS ix_incidents_analysis_gin "
        f"ON {_TABLE} USING GIN (analysis jsonb_path_ops)"
    ))


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    _set_lock_timeout()
    for name in ("ix_incidents_analysis_gin", "ix_incidents_created_at",
                 "ix_incidents_status", "uq_incidents_incident_id"):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    for col in _JSON_COLUMNS:
        op.execute(sa.text(
            f"ALTER TABLE {_TABLE} ALTER COLUMN {col} TYPE json USING {col}::json"
        ))
