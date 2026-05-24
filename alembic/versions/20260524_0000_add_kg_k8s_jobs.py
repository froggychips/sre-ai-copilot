"""add kg_k8s_jobs table (Job + CronJob coverage)

Revision ID: 20260524_0000
Revises: 20260523_0000
Create Date: 2026-05-24 00:00:00.000000

KG Coverage #1: новая таблица для k8s Job и CronJob. До этой миграции KG
не видел backup CronJob'ов (push-s3, etcd-snapshot) и failed alembic-
migrations — критичный sourcing-gap для post-mortem'ов.

Дизайн-решение: отдельная table вместо `kind='job'` в kg_services —
job-counters / cron-schedule полей у Service нет, смешивать health-метрики
плохо (Job завершается → metric_signals=0, не означает down).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260524_0000"
down_revision = "20260523_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_k8s_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("owner_service_name", sa.String(), nullable=True),
        sa.Column("schedule", sa.String(), nullable=True),
        sa.Column(
            "suspended", sa.Boolean(), nullable=False, server_default="false",
        ),
        sa.Column("last_schedule_time", sa.DateTime(), nullable=True),
        sa.Column("last_successful_time", sa.DateTime(), nullable=True),
        sa.Column("succeeded_count", sa.Integer(), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=True),
        sa.Column("active_count", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("completion_time", sa.DateTime(), nullable=True),
        sa.Column("last_pod_exit_code", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "namespace", "name", "kind", name="uq_kg_k8s_job_ns_name_kind",
        ),
    )
    op.create_index(
        "ix_kg_k8s_jobs_namespace", "kg_k8s_jobs", ["namespace"],
    )
    op.create_index(
        "ix_kg_k8s_jobs_name", "kg_k8s_jobs", ["name"],
    )
    op.create_index(
        "ix_kg_k8s_jobs_kind", "kg_k8s_jobs", ["kind"],
    )
    op.create_index(
        "ix_kg_k8s_jobs_owner_service_name",
        "kg_k8s_jobs", ["owner_service_name"],
    )
    op.create_index(
        "ix_kg_k8s_job_kind_ns", "kg_k8s_jobs", ["kind", "namespace"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_k8s_job_kind_ns", table_name="kg_k8s_jobs")
    op.drop_index("ix_kg_k8s_jobs_owner_service_name", table_name="kg_k8s_jobs")
    op.drop_index("ix_kg_k8s_jobs_kind", table_name="kg_k8s_jobs")
    op.drop_index("ix_kg_k8s_jobs_name", table_name="kg_k8s_jobs")
    op.drop_index("ix_kg_k8s_jobs_namespace", table_name="kg_k8s_jobs")
    op.drop_table("kg_k8s_jobs")
