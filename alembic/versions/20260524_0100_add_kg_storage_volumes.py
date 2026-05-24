"""add kg storage volumes (PVC/PV) and volume edges

Revision ID: 20260524_0100
Revises: 20260524_0000
Create Date: 2026-05-24 00:00:00.000000

KG Coverage #2: PVC/PV/storage signals. Самый ценный пункт по value/cost —
ClickHouse / Postgres «упал» = в 95% случаев диск кончился. До этой
миграции KG не знал про storage слой.

Две новые таблицы:

  * kg_storage_volumes — узлы PVC и PV. `kind` различает
    ('pvc' | 'pv'), `name` уникален в (namespace, name, kind) — у PV
    namespace всегда '' (cluster-scoped); у PVC — реальный ns.
    Метаданные: capacity_bytes (нормализованный размер), storage_class,
    access_modes, phase (Bound/Pending/Released/Available/Failed для PV;
    Bound/Pending/Lost для PVC). `disk_pct` — последний known
    использования из VM (опциональный — может быть NULL если scrape
    не покрывает kubelet_volume_stats_*).

  * kg_volume_edges — гетерогенные edges (Service→PVC, PVC→PV). Не reuse
    kg_service_edges, потому что там src_id/dst_id FK на kg_services —
    логически разные node-типы. `src_kind`/`dst_kind` явно различают
    откуда узел: 'service' | 'pvc' | 'pv'. Уникальность по
    (src_kind, src_id, dst_kind, dst_id, kind).

FK без ondelete=CASCADE — случайная чистка storage_volumes не должна
утянуть edges-историю; чистится через `kg_drift_cleanup` (волюмы) и
будущий `kg_volume_edges_decay` (edges не подтверждённые N дней).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260524_0100"
down_revision = "20260524_0000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── kg_storage_volumes ─────────────────────────────────────────────
    op.create_table(
        "kg_storage_volumes",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        # 'pvc' | 'pv'. У PVC namespace = реальный ns; у PV namespace = ''
        # (cluster-scoped), но колонка nullable=False с server_default '' —
        # это избавляет от if-блоков в JOIN'ах.
        sa.Column("kind", sa.String(), nullable=False, index=True),
        sa.Column(
            "namespace",
            sa.String(),
            nullable=False,
            server_default="",
            index=True,
        ),
        sa.Column("name", sa.String(), nullable=False, index=True),
        # capacity в bytes (parsed из "100Gi"/"500Mi"/etc.). На PVC берётся
        # из spec.resources.requests.storage; на PV — из spec.capacity.storage.
        # NULL если parsing не удался (см. _parse_capacity_to_bytes).
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_class", sa.String(), nullable=True, index=True),
        # PV-уровень: phase = Bound/Pending/Released/Available/Failed.
        # PVC-уровень: phase = Bound/Pending/Lost. Хранится строкой как k8s.
        sa.Column("phase", sa.String(), nullable=True, index=True),
        # Список access modes из spec.accessModes (ReadWriteOnce/ReadOnlyMany/
        # ReadWriteMany/ReadWriteOncePod). JSON-list для forward-compat.
        sa.Column("access_modes", sa.JSON(), nullable=True),
        # PVC.spec.volumeName → имя bound PV. NULL у Pending PVC и у PV-узлов.
        # Дублирует bound_to edge — оставляем колонку для быстрых выборок
        # «какой PV у этого PVC» без JOIN-а.
        sa.Column("volume_name", sa.String(), nullable=True),
        # disk_pct % использования из kubelet_volume_stats_* (опционально,
        # см. STORAGE_METRICS_ENABLED). NULL означает: либо метрика не
        # включена, либо scrape config не покрывает этот volume.
        sa.Column("disk_pct", sa.Float(), nullable=True),
        # Произвольный JSON: pv.spec.csi, finalizers, claimRef для PV, и т.п.
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "kind", "namespace", "name",
            name="uq_kg_storage_volumes_kind_ns_name",
        ),
    )
    op.create_index(
        "ix_kg_storage_volumes_kind_ns",
        "kg_storage_volumes",
        ["kind", "namespace"],
    )

    # ── kg_volume_edges ────────────────────────────────────────────────
    op.create_table(
        "kg_volume_edges",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("src_kind", sa.String(), nullable=False, index=True),
        sa.Column("src_id", sa.Integer(), nullable=False, index=True),
        sa.Column("dst_kind", sa.String(), nullable=False, index=True),
        sa.Column("dst_id", sa.Integer(), nullable=False, index=True),
        # 'uses_volume' (Service → PVC) | 'bound_to' (PVC → PV)
        sa.Column("kind", sa.String(), nullable=False, index=True),
        sa.Column("discovered_by", sa.String(), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True, index=True),
        sa.UniqueConstraint(
            "src_kind", "src_id", "dst_kind", "dst_id", "kind",
            name="uq_kg_volume_edge_src_dst_kind",
        ),
    )
    op.create_index(
        "ix_kg_volume_edges_src",
        "kg_volume_edges",
        ["src_kind", "src_id"],
    )
    op.create_index(
        "ix_kg_volume_edges_dst",
        "kg_volume_edges",
        ["dst_kind", "dst_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_volume_edges_dst", table_name="kg_volume_edges")
    op.drop_index("ix_kg_volume_edges_src", table_name="kg_volume_edges")
    op.drop_table("kg_volume_edges")

    op.drop_index(
        "ix_kg_storage_volumes_kind_ns",
        table_name="kg_storage_volumes",
    )
    op.drop_table("kg_storage_volumes")
