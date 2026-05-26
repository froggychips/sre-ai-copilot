"""kg_alerts: добавить колонку last_notified_at

Revision ID: 20260526_0100
Revises: 20260525_0100
Create Date: 2026-05-26 01:00:00.000000

Хронические алерты (KubePodCrashLooping и т.п.) имеют fired_at недели
назад, поэтому окно «fired_at BETWEEN started_at AND started_at+30m»
давало 0% attribution в deploy_incident_correlation_section.

last_notified_at обновляется при каждом AM webhook (repeat_interval
срабатывает раз в 4-24h) — этот timestamp реально попадает в окно деплоя.

Старые строки: last_notified_at = NULL (будет заполняться при следующем
webhook). Индекс — для корреляционного JOIN-а в stats_digest.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260526_0100"
down_revision = "20260525_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_alerts",
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_kg_alert_last_notified_at",
        "kg_alerts",
        ["last_notified_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_alert_last_notified_at", table_name="kg_alerts")
    op.drop_column("kg_alerts", "last_notified_at")
