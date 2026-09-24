"""kg_k8s_job_runs: история состояний Job-ов

Revision ID: 20260924_0200
Revises: 20260924_0100
Create Date: 2026-09-24 02:00:00.000000

`kg_k8s_jobs` хранит последнее состояние Job-а и теряет то, что нужно при
разборе инцидента: упавший migrate-job перезаписывается следующим запуском с
тем же именем или удаляется вместе с namespace-ом. Новая таблица — строка на
каждое изменение статуса (пишет k8s_jobs_sync), retention 30 дней.

Новая пустая таблица — без блокировок и без backfill: прошлое состояние Job-ов
восстановить не из чего, история начинается с первого тика после выката.
Порядок выката не важен: без таблицы sync пишет только снимок (чтение истории
в begin_nested, сбой → history выключена на тик). Downgrade безопасен —
kg_k8s_jobs не затрагивается.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0200"
down_revision = "20260924_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_k8s_job_runs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("uid", sa.String(), nullable=True),
        sa.Column("owner_service_name", sa.String(), nullable=True),
        sa.Column("succeeded_count", sa.Integer(), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=True),
        sa.Column("active_count", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("completion_time", sa.DateTime(), nullable=True),
        sa.Column("last_pod_exit_code", sa.Integer(), nullable=True),
        sa.Column("condition_type", sa.String(), nullable=True),
        sa.Column("condition_reason", sa.String(), nullable=True),
        sa.Column("condition_message", sa.Text(), nullable=True),
        sa.Column("disappeared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_kg_k8s_job_runs_ns_observed", _TABLE, ["namespace", "observed_at"])
    op.create_index("ix_kg_k8s_job_runs_ns_name", _TABLE, ["namespace", "name", "id"])


def downgrade() -> None:
    op.drop_index("ix_kg_k8s_job_runs_ns_name", table_name=_TABLE)
    op.drop_index("ix_kg_k8s_job_runs_ns_observed", table_name=_TABLE)
    op.drop_table(_TABLE)
