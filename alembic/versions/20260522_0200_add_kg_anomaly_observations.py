"""add kg_anomaly_observations (rolling z-score аномалии per service)

Revision ID: 20260522_0200
Revises: 20260522_0100
Create Date: 2026-05-22 02:00:00.000000

Beat-task `kg_anomaly_detection_task` каждые 10 минут пробегает по
kg_service_health и для каждой из 5 метрик (cpu_pct, mem_pct,
restarts_rate, http_5xx_rate, p95_latency_ms) считает rolling z-score.
При |z|>3 — пишем строку в эту таблицу.

UNIQUE(service_id, ts, metric) обеспечивает идемпотентность: повторный
beat-tick за тот же snapshot не плодит дубликаты. notified=false до тех
пор, пока вторая фаза (Discord) не пометит запись отосланной.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260522_0200"
down_revision = "20260522_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_anomaly_observations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("kg_services.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("baseline_mean", sa.Float(), nullable=True),
        sa.Column("baseline_stddev", sa.Float(), nullable=True),
        sa.Column("z_score", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column(
            "notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "service_id", "ts", "metric",
            name="uq_kg_anomaly_obs_service_ts_metric",
        ),
    )
    op.create_index(
        "ix_kg_anomaly_obs_service_ts",
        "kg_anomaly_observations",
        ["service_id", sa.text("ts DESC")],
    )
    op.create_index(
        "ix_kg_anomaly_obs_severity_ts",
        "kg_anomaly_observations",
        ["severity", sa.text("ts DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kg_anomaly_obs_severity_ts",
        table_name="kg_anomaly_observations",
    )
    op.drop_index(
        "ix_kg_anomaly_obs_service_ts",
        table_name="kg_anomaly_observations",
    )
    op.drop_table("kg_anomaly_observations")
