"""kg_services.stale_class column (active / expected_stale / suspicious_stale)

Revision ID: 20260524_0200
Revises: 20260524_0100
Create Date: 2026-05-24 02:00:00.000000

KG Coverage #4: вынос локальной эвристики `_classify_stale(name, ns)` из
`app/services/stats_digest.py` в first-class column на ``kg_services``,
чтобы фильтр `stale_class IN ('suspicious_stale')` был доступен в любом
SQL/dashboard без kubectl-обхода.

Хранится как String (не PG enum) ради sqlite-compat тестов. nullable=True —
backfill идёт лениво при следующем `kg_sync.sync_namespace`; миграция не
блокирует rollout.

down_revision = 20260524_0100 (storage volumes, PR #84) — оба нагенерили
``20260524_0100`` ID параллельно; rebase 2026-05-24 переименовал stale_class
на 0200, чтобы линеаризовать историю.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260524_0200"
down_revision = "20260524_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_services",
        sa.Column("stale_class", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_kg_services_stale_class",
        "kg_services",
        ["stale_class"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_services_stale_class", table_name="kg_services")
    op.drop_column("kg_services", "stale_class")
