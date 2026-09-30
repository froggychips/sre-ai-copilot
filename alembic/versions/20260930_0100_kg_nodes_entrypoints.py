"""kg_nodes + kg_entrypoints: зоны нод и точки входа трафика

Revision ID: 20260930_0100
Revises: 20260924_0300
Create Date: 2026-09-30 01:00:00.000000

Граф не знал, в какой зоне/датацентре живёт нода, и через какую ноду
публичный host попадает в кластер (MetalLB L2, externalTrafficPolicy: Local —
IP балансировщика совпадает с адресом ноды). Две новые таблицы, пишет
k8s_nodes_sync (kubectl get nodes / ingress / svc + DNS-резолв host-ов).

Новые пустые таблицы — без блокировок и без backfill: данные появятся с
первого тика синка. Порядок выката не важен: без таблиц источник kg_nodes в
сборщике контекста падает в свой savepoint и помечается failed, остальные
источники на месте. Downgrade безопасен — существующие таблицы не
затрагиваются.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260930_0100"
down_revision = "20260924_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("zone", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("internal_ip", sa.String(), nullable=True),
        sa.Column("external_ip", sa.String(), nullable=True),
        sa.Column("addresses", sa.JSON(), nullable=True),
        sa.Column("roles", sa.JSON(), nullable=True),
        sa.Column("unschedulable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("labels_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("name", name="uq_kg_nodes_name"),
    )
    op.create_table(
        "kg_entrypoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("resolved_ips", sa.JSON(), nullable=True),
        sa.Column("lb_services", sa.JSON(), nullable=True),
        sa.Column("entry_nodes", sa.JSON(), nullable=True),
        sa.Column("ingress_classes", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("host", name="uq_kg_entrypoints_host"),
    )


def downgrade() -> None:
    op.drop_table("kg_entrypoints")
    op.drop_table("kg_nodes")
