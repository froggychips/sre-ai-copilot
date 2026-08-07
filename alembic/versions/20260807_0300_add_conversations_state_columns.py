"""conversations: добираем current_state / retry_count (дрейф модель↔миграции)

Revision ID: 20260807_0300
Revises: 20260610_0100
Create Date: 2026-08-07 01:00:00.000000

`app/models/__init__.py` объявляет у Conversation колонки `current_state`
(String, default="OPEN", NOT NULL) и `retry_count` (Integer, default=0,
NOT NULL), но initial-миграция 20240101_0000 создала таблицу БЕЗ них, и ни
одна последующая их не добавила. Свежий деплой через `alembic upgrade head`
падал UndefinedColumn на первом же INSERT (app/repository.py:
create_conversation) и в app/celery_worker.py (conv.current_state).
Существующие окружения работали только потому, что таблицу им когда-то
создал `Base.metadata.create_all()` в обход alembic.

Поэтому миграция идемпотентна: колонки могли уже появиться через
create_all() — проверяем наличие через inspector и добавляем только
недостающие. server_default нужен, чтобы backfill-ить существующие строки
при NOT NULL; оставляем его и после (безвреден: python-side default модели
его перекрывает на INSERT, а autogenerate server_default по умолчанию
не сравнивает).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260807_0300"
down_revision = "20260807_0200"
branch_labels = None
depends_on = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns("conversations")}


def upgrade() -> None:
    cols = _existing_columns()
    if "current_state" not in cols:
        op.add_column(
            "conversations",
            sa.Column(
                "current_state",
                sa.String(),
                nullable=False,
                server_default="OPEN",
            ),
        )
    if "retry_count" not in cols:
        op.add_column(
            "conversations",
            sa.Column(
                "retry_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    cols = _existing_columns()
    to_drop = [c for c in ("retry_count", "current_state") if c in cols]
    if not to_drop:
        return
    # batch_alter_table — ради sqlite (ALTER ... DROP COLUMN до 3.35 нет);
    # на Postgres разворачивается в обычные ALTER TABLE.
    with op.batch_alter_table("conversations") as batch_op:
        for col in to_drop:
            batch_op.drop_column(col)
