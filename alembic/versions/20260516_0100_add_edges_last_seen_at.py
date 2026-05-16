"""C1: kg_service_edges.last_seen_at + backfill

Revision ID: 20260516_0100
Revises: 20260516_0000
Create Date: 2026-05-16 12:00:00.000000

C1 из roadmap: TTL/decay. Без `last_seen_at` edge живёт навсегда, даже
если сервис давно удалил env-var → KG показывает stale-зависимости.
last_seen_at обновляется в `populator.upsert_edge` при каждом sync.
Backfill в этой же миграции — set last_seen_at = created_at, чтобы
существующие edges не выглядели "никогда не подтверждены".
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_0100"
down_revision = "20260516_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_service_edges",
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_kg_service_edges_last_seen_at",
        "kg_service_edges",
        ["last_seen_at"],
    )
    # Backfill: для существующих edges стартанём last_seen_at = created_at.
    # Это не делает edge "свежим" — просто фиксирует "был жив тогда".
    op.execute(
        "UPDATE kg_service_edges SET last_seen_at = created_at "
        "WHERE last_seen_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_kg_service_edges_last_seen_at", table_name="kg_service_edges")
    op.drop_column("kg_service_edges", "last_seen_at")
