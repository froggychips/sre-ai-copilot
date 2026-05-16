"""service health score column + index

Revision ID: 20260516_0200
Revises: 20260516_0100
Create Date: 2026-05-16 21:00:00.000000

Per ChatGPT review #4.3: service health score per service.
Composite score из open_alerts × severity + crashloop_count + recent_restarts.
Хранится в kg_services.health_score (Float [0, 1], где 1.0 = perfect health).

Periodic refresh through `kg_health_recompute` beat task. Используется
в kg_fragile_top для «истинного» ранжирования (раньше было только по
inbound count, что давало bias на NATS clusters).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_0200"
down_revision = "20260516_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_services",
        sa.Column("health_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "kg_services",
        sa.Column("health_computed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_kg_services_health_score",
        "kg_services",
        ["health_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_services_health_score", table_name="kg_services")
    op.drop_column("kg_services", "health_computed_at")
    op.drop_column("kg_services", "health_score")
