"""llm_spend_ledger: суточный расход на LLM, durable

Revision ID: 20260917_0100
Revises: 20260909_0100
Create Date: 2026-09-17 01:00:00.000000

Счётчик расхода сначала жил в Redis, и это было ошибкой. Redis здесь поднят
с `maxmemory 256mb` и `maxmemory-policy allkeys-lru` (`k8s/redis.yaml`): под
давлением памяти вытесняется любой ключ, включая этот. Пропавший счётчик
читается как «потрачено 0» и выдаёт полный суточный бюджет заново — то есть
предохранитель открывается сам, тихо и ровно тогда, когда система под
нагрузкой.

Строка в сутки — таблица растёт на 365 записей в год, индекса кроме
первичного ключа не нужно.

`spent_micro_usd` — микродоллары целым: расход накапливается тысячами
сложений, и дробное сложение копило бы ошибку двоичного округления. BIGINT
вмещает 9.2e18 микродолларов — потолок в девять триллионов долларов в сутки
запасом можно считать достаточным.

CREATE TABLE новой таблицы: писателей нет по определению, блокировать
нечего.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260917_0100"
down_revision = "20260909_0100"
branch_labels = None
depends_on = None

_TABLE = "llm_spend_ledger"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column(
            "spent_micro_usd",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        # naive-UTC: вся схема живёт без timezone, и смешивать timestamptz с
        # timestamp в одной базе значит считать окна по разным зонам.
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
