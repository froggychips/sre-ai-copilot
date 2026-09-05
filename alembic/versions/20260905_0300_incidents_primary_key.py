"""incidents: первичный ключ, которого не было

Revision ID: 20260905_0300
Revises: 20260905_0200
Create Date: 2026-09-05 03:00:00.000000

`pg_constraint` для `incidents` на 05.09.2026 — пустой список. Ни одного
констрейнта, включая PRIMARY KEY: модель объявляет `id = Column(Integer,
primary_key=True)`, а в базе колонка `id` — просто integer с sequence.
Миграция 20260819_0100 честно записала «индексов на таблице не было
ВООБЩЕ» и завела уникальный индекс на `incident_id`, но PK так и не
появился.

Пока таблица пуста (пайплайн за `LLM_PIPELINE_ENABLED=false`), это ничего
не стоит. Стоить начнёт в момент, когда Incident станет детерминированным
объектом из alert-групп — то есть на следующем шаге. Заводить PK на
наполненной таблице — уже ACCESS EXCLUSIVE с полным сканом; сейчас —
мгновенно.

`incident_id` при этом остаётся уникальным индексом, а не вторым PK: это
бизнес-ключ (по нему ищут снаружи), а `id` — суррогатный для FK.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0300"
down_revision = "20260905_0200"
branch_labels = None
depends_on = None

_TABLE = "incidents"
_PK = "pk_incidents"
_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() != "postgresql":
        return          # sqlite в тестах: PK создаётся из модели
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    # Идемпотентно: инсталляция, где PK всё-таки есть, не должна падать.
    op.execute(sa.text(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = '{_TABLE}'::regclass AND contype = 'p'
            ) THEN
                ALTER TABLE {_TABLE} ADD CONSTRAINT {_PK} PRIMARY KEY (id);
            END IF;
        END $$;
    """))


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_PK}"))
