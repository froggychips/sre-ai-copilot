"""discord_dedup: cross-replica состояние PATCH-dedup

Revision ID: 20260610_0100
Revises: 20260526_0100
Create Date: 2026-06-10 17:00:00.000000

sre-ai-api в 2 репликах, а `_recent_enriched` (PATCH-dedup для
send_enriched_alert) — per-process dict. AM-вебхук балансится между
подами → дедуп-промах → дубль-POST critical с повторным mention
(прецедент 2026-06-10: PreprodRestartsSpike в 16:16 и 16:31).

Таблица — shared-состояние дедупа: одна строка на content-key
(sha1 от alertname|ns|service|severity) в TTL-окне. Protухшие строки
удаляются opportunistic-purge'ем при каждом get_fresh.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260610_0100"
down_revision = "20260526_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discord_dedup",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("msg_id", sa.String(32), nullable=False),
        sa.Column("webhook_url", sa.String(512), nullable=False),
        sa.Column("embed", sa.JSON(), nullable=True),
        sa.Column("first_ts", sa.DateTime(), nullable=False),
        sa.Column("last_ts", sa.DateTime(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("alertname", sa.String(255), nullable=True),
        sa.Column("namespace", sa.String(255), nullable=True),
        sa.Column("service", sa.String(255), nullable=True),
        sa.Column("severity", sa.String(32), nullable=True),
    )
    op.create_index("ix_discord_dedup_first_ts", "discord_dedup", ["first_ts"])


def downgrade() -> None:
    op.drop_index("ix_discord_dedup_first_ts", table_name="discord_dedup")
    op.drop_table("discord_dedup")
