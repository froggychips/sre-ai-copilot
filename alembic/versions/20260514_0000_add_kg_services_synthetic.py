"""add kg_services.synthetic flag + backfill known synthetic patterns

Revision ID: 20260514_0000
Revises: 20260513_0000
Create Date: 2026-05-14 16:00:00.000000

KG-orphan-reduction PR A.

Многие kg_services по дизайну изолированы и никогда не должны иметь edges:
backup-cron'ы, NATS-tools (box/client-box/exporter), seq/redis-exporter,
прочие *-cron. Раньше они засчитывались в Orphan %-метрику и раздували
её до 33%. Теперь — флаг `synthetic`, который ставится в kg_sync и
исключается из orphan-counter (`stats_digest.kg_quality_section`).

Backfill в этой же миграции — чтобы эффект был мгновенный после rollout,
без ожидания следующего kg_sync.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260514_0000"
down_revision = "20260513_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_services",
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Backfill: пометить existing synthetic-services сразу.
    # Паттерны намеренно узкие чтобы не зацепить настоящие сервисы.
    op.execute(
        """
        UPDATE kg_services SET synthetic = true
        WHERE name LIKE '%-db-backup'
           OR name LIKE '%-cron'
           OR name IN (
               'nats-box',
               'nats-client-box',
               'nats-exporter-prometheus-nats-exporter',
               'seq',
               'redis-exporter'
           )
        """
    )


def downgrade() -> None:
    op.drop_column("kg_services", "synthetic")
