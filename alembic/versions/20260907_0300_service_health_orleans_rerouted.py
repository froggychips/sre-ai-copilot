"""kg_service_health: orleans_rerouted_rate — пересылка отдельно от сбоев

Revision ID: 20260907_0300
Revises: 20260907_0200
Create Date: 2026-09-07 03:00:00.000000

`messaging_rerouted` — пересылка сообщения на другой силос (активация
переехала, кэш директории устарел). Это не отказ доставки, поэтому 1.0.10
убрал его из `orleans_messaging_fault_rate`; но терять сигнал нельзя: рераны
растут раньше сбоев — при смерти силоса, при перекатке, при шторме активаций.
Своя nullable-колонка, та же семантика нуля, что у остальных orleans_*.

ADD COLUMN без DEFAULT — только метаданные, но ACCESS EXCLUSIVE нужен: на
таблице с постоянными короткими запросами metrics_sync / детектора за
lock_timeout=15s он не берётся (1.0.9). Перед миграцией глушить писателей:
`kubectl -n sre-ai scale deploy copilot-worker copilot-beat --replicas=0`.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260907_0300"
down_revision = "20260907_0200"
branch_labels = None
depends_on = None

_TABLE = "kg_service_health"
_COLUMN = "orleans_rerouted_rate"
_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.drop_column(_TABLE, _COLUMN)
