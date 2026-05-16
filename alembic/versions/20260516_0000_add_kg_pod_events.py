"""add kg_pod_events table (A4)

Revision ID: 20260516_0000
Revises: 20260514_0000
Create Date: 2026-05-16 11:00:00.000000

A4 из KG roadmap: параллельный signal к kg_alerts. AlertManager-based
kg_alerts ловят только то, что отрендерилось как PromQL alert; диагностические
события на pod-уровне (OOMKilled, FailedScheduling, ImagePullBackOff,
FailedMount, BackOff, NodeNotReady) теряются. Эти события — gold для root
cause при KubePodCrashLooping.

Идемпотентность — uq по event_uid (k8s UID события).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_0000"
down_revision = "20260514_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_pod_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("pod_name", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("event_uid", sa.String(), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["service_id"], ["kg_services.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kg_pod_events_id", "kg_pod_events", ["id"])
    op.create_index("ix_kg_pod_events_service_id", "kg_pod_events", ["service_id"])
    op.create_index("ix_kg_pod_events_namespace", "kg_pod_events", ["namespace"])
    op.create_index("ix_kg_pod_events_pod_name", "kg_pod_events", ["pod_name"])
    op.create_index("ix_kg_pod_events_reason", "kg_pod_events", ["reason"])
    op.create_index("ix_kg_pod_events_first_seen", "kg_pod_events", ["first_seen"])
    op.create_index(
        "ix_kg_pod_events_event_uid", "kg_pod_events", ["event_uid"], unique=True,
    )
    op.create_index(
        "ix_kg_pod_event_service_time", "kg_pod_events", ["service_id", "first_seen"],
    )
    op.create_index(
        "ix_kg_pod_event_ns_reason_time",
        "kg_pod_events",
        ["namespace", "reason", "first_seen"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_pod_event_ns_reason_time", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_event_service_time", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_event_uid", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_first_seen", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_reason", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_pod_name", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_namespace", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_service_id", table_name="kg_pod_events")
    op.drop_index("ix_kg_pod_events_id", table_name="kg_pod_events")
    op.drop_table("kg_pod_events")
