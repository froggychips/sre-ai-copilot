"""add kg_log_observations (error/fatal log aggregates per service per window)

Revision ID: 20260522_0300
Revises: 20260522_0200
Create Date: 2026-05-22 03:00:00.000000

Материализация error/fatal/warning логов из Seq в KG. Источник —
несколько Seq-инстансов (prod / preprod / preupdate), beat-task
`kg_seq_logs_sync` тянет count событий по level за окно ~10 мин и
агрегирует per service per level.

`service_id` NULLABLE: если по Application/service-тэгу из Seq мы не
смогли сматчить запись в `kg_services` — пишем строку с service_id=NULL
(не теряем сигнал; запись остаётся атрибутирована через namespace/source).

UNIQUE(service_id, ts, level, source) — для идемпотентности повторного
beat tick'а в одном окне. ON CONFLICT DO UPDATE count=excluded.count
живёт в SQL-апсёрте `seq_logs_sync.py`.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260522_0300"
down_revision = "20260522_0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_log_observations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("kg_services.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("top_message_hash", sa.String(), nullable=True),
        sa.Column("sample_message", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "service_id", "ts", "level", "source",
            name="uq_kg_log_obs_service_ts_level_source",
        ),
    )
    op.create_index(
        "ix_kg_log_obs_service_ts",
        "kg_log_observations",
        ["service_id", sa.text("ts DESC")],
    )
    op.create_index(
        "ix_kg_log_obs_level_ts",
        "kg_log_observations",
        ["level", sa.text("ts DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_log_obs_level_ts", table_name="kg_log_observations")
    op.drop_index("ix_kg_log_obs_service_ts", table_name="kg_log_observations")
    op.drop_table("kg_log_observations")
