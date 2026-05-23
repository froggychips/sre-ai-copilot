"""add extras (JSON) column to kg_anomaly_observations

Revision ID: 20260523_0000
Revises: 20260522_0400
Create Date: 2026-05-23 00:00:00.000000

Адаптивный threshold (robust z через MAD + опциональный seasonal baseline)
нуждается в полях для отладки: какой method был использован, какие точки
попали в baseline и т.п. Кладём в JSON-колонку `extras`, чтобы не плодить
flat-колонки под debug-данные.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260523_0000"
down_revision = "20260522_0400"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_anomaly_observations",
        sa.Column("extras", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kg_anomaly_observations", "extras")
