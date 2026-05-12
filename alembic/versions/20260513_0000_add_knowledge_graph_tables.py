"""add knowledge graph tables (kg_services, kg_deployments, kg_alerts, kg_service_edges)

Revision ID: 20260513_0000
Revises: 20260511_0000
Create Date: 2026-05-13 00:00:00.000000

До этой миграции таблицы KG создавались через Base.metadata.create_all()
при старте (SQLite-only local dev). Здесь они переводятся под Alembic-контроль
для корректной работы с PostgreSQL в k8s.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260513_0000"
down_revision = "20260511_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_services",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False, index=True),
        sa.Column("namespace", sa.String(), nullable=False, index=True),
        sa.Column("team_owner", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("namespace", "name", name="uq_kg_service_ns_name"),
    )

    op.create_table(
        "kg_deployments",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("kg_services.id"), nullable=False, index=True),
        sa.Column("sha", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("buildtype_id", sa.String(), nullable=True),
        sa.Column("build_number", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=True),
    )
    op.create_index("ix_kg_deploy_service_time", "kg_deployments", ["service_id", "started_at"])

    op.create_table(
        "kg_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("kg_services.id"), nullable=True, index=True),
        sa.Column("alertname", sa.String(), nullable=False, index=True),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True, unique=True, index=True),
        sa.Column("fired_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("incident_id", sa.String(), nullable=True, index=True),
        sa.Column("raw", sa.JSON(), nullable=True),
    )
    op.create_index("ix_kg_alert_service_time", "kg_alerts", ["service_id", "fired_at"])

    op.create_table(
        "kg_service_edges",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("src_id", sa.Integer(), sa.ForeignKey("kg_services.id"), nullable=False, index=True),
        sa.Column("dst_id", sa.Integer(), sa.ForeignKey("kg_services.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("weight", sa.Integer(), default=1),
        sa.Column("discovered_by", sa.String(), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("src_id", "dst_id", "kind", name="uq_kg_edge_src_dst_kind"),
    )


def downgrade() -> None:
    op.drop_table("kg_service_edges")
    op.drop_index("ix_kg_alert_service_time", table_name="kg_alerts")
    op.drop_table("kg_alerts")
    op.drop_index("ix_kg_deploy_service_time", table_name="kg_deployments")
    op.drop_table("kg_deployments")
    op.drop_table("kg_services")
