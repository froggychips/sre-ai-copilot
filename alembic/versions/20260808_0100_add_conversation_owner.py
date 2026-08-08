"""conversations: владелец диалога (owner_sub) — закрываем IDOR на /copilot

Revision ID: 20260808_0100
Revises: 20260807_0400
Create Date: 2026-08-08 01:00:00.000000

У `Conversation` не было владельца, а `/copilot` принимал `conversation_id`
из тела запроса и никак не сверял его с текущим пользователем. Любой
аутентифицированный пользователь мог передать чужой UUID: сообщение
дописывалось в чужой диалог, `generate_reply` двигал его state machine, а
результат забирался через `/jobs/{task_id}`. Колонка `owner_sub` (значение
JWT-claim `sub`) — то, по чему теперь проверяется принадлежность.

nullable=True осознанно: у существующих строк владельца нет и вычислить его
неоткуда (в схеме нет ни одного следа автора — ни в `conversations`, ни в
`messages`). Backfill невозможен, поэтому колонка остаётся nullable, а
NULL-строки приложение трактует как ЧУЖИЕ — см. app/repository.py.
NOT NULL можно будет поставить отдельной миграцией, когда legacy-строки
будут вычищены или усыновлены оператором вручную.

Миграция идемпотентна (колонка/индекс могли появиться через
`Base.metadata.create_all()` в обход alembic — этим окружениям тут уже
досталось, см. 20260807_0300) и совместима с postgres и sqlite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260808_0100"
down_revision = "20260807_0400"
branch_labels = None
depends_on = None

_TABLE = "conversations"
_COLUMN = "owner_sub"
# Имя, которое SQLAlchemy выводит для `index=True` у Conversation.owner_sub.
# Держим его тем же, чтобы autogenerate не видел дрейфа.
_INDEX = "ix_conversations_owner_sub"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {idx["name"] for idx in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _existing_columns():
        # server_default НЕ нужен: колонка nullable, backfill-ить нечем.
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))
    if _INDEX not in _existing_indexes():
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    # Индекс сносим ПЕРВЫМ и вне batch-блока: на sqlite batch_alter_table
    # пересоздаёт таблицу вместе с отражёнными индексами, и индекс по уже
    # удалённой колонке уронил бы пересоздание.
    if _INDEX in _existing_indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN not in _existing_columns():
        return
    # batch_alter_table — ради sqlite (ALTER ... DROP COLUMN до 3.35 нет);
    # на Postgres разворачивается в обычный ALTER TABLE.
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
