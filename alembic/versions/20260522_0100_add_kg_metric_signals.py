"""add kg metric signals (service_health, cluster_observations, ingress_observations, signal_aggregates)

Revision ID: 20260522_0100
Revises: 20260516_0200
Create Date: 2026-05-22 01:00:00.000000

Материализация time-series метрик из VictoriaMetrics в KG. До этой миграции
VM-клиент использовался on-demand в pipeline и stats_digest — никаких
исторических снимков. Новые 4 таблицы:

  * kg_service_health        — per-service snapshot (cpu/mem/restarts/5xx/p95)
                               каждые ~10 мин.
  * kg_cluster_observations  — single global snapshot (узлы/поды/алерты) каждые
                               ~5 мин.
  * kg_ingress_observations  — per ingress endpoint snapshot каждые ~10 мин.
  * kg_signal_aggregates     — pre-compute per service per 24h (deploys/alerts/
                               pod_events/slo_burn).

FK на kg_services БЕЗ ondelete=CASCADE — случайная чистка services не должна
утянуть метрик-историю. Уникальность по (service_id, ts) / (ts) / (ingress, host,
path, ts) / (service_id, window_end) — beat-task должен быть идемпотентен.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260522_0100"
down_revision = "20260516_0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── kg_service_health ──────────────────────────────────────────────
    op.create_table(
        "kg_service_health",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("kg_services.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column("restarts_rate", sa.Float(), nullable=True),
        sa.Column("http_5xx_rate", sa.Float(), nullable=True),
        sa.Column("p95_latency_ms", sa.Float(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "service_id", "ts", name="uq_kg_service_health_service_ts",
        ),
    )
    op.create_index(
        "ix_kg_service_health_service_ts",
        "kg_service_health",
        ["service_id", sa.text("ts DESC")],
    )
    op.create_index(
        "ix_kg_service_health_ts",
        "kg_service_health",
        [sa.text("ts DESC")],
    )

    # ── kg_cluster_observations ────────────────────────────────────────
    op.create_table(
        "kg_cluster_observations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column("disk_peak_pct", sa.Float(), nullable=True),
        sa.Column("pods_running", sa.Integer(), nullable=True),
        sa.Column("pods_pending", sa.Integer(), nullable=True),
        sa.Column("pods_failed", sa.Integer(), nullable=True),
        sa.Column("crashloops", sa.Integer(), nullable=True),
        sa.Column("deploy_mismatch", sa.Integer(), nullable=True),
        sa.Column("alerts_critical", sa.Integer(), nullable=True),
        sa.Column("alerts_warning", sa.Integer(), nullable=True),
        sa.Column("alerts_prod", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.UniqueConstraint("ts", name="uq_kg_cluster_obs_ts"),
    )
    op.create_index(
        "ix_kg_cluster_obs_ts",
        "kg_cluster_observations",
        [sa.text("ts DESC")],
    )

    # ── kg_ingress_observations ────────────────────────────────────────
    op.create_table(
        "kg_ingress_observations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("ingress_name", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("kg_services.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("p95_latency_ms", sa.Float(), nullable=True),
        sa.Column("p99_latency_ms", sa.Float(), nullable=True),
        sa.Column("rps", sa.Float(), nullable=True),
        sa.Column("error_5xx_rate", sa.Float(), nullable=True),
        sa.Column("error_4xx_rate", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "ingress_name", "host", "path", "ts",
            name="uq_kg_ingress_obs_ingress_host_path_ts",
        ),
    )
    op.create_index(
        "ix_kg_ingress_obs_ingress_ts",
        "kg_ingress_observations",
        ["ingress_name", sa.text("ts DESC")],
    )

    # ── kg_signal_aggregates ───────────────────────────────────────────
    op.create_table(
        "kg_signal_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("kg_services.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=True),
        sa.Column("deploy_count", sa.Integer(), nullable=True),
        sa.Column("deploy_failure_pct", sa.Float(), nullable=True),
        sa.Column("alert_open_count", sa.Integer(), nullable=True),
        sa.Column("alert_ttr_p50_min", sa.Float(), nullable=True),
        sa.Column("pod_event_count", sa.Integer(), nullable=True),
        sa.Column("top_event_reason", sa.String(), nullable=True),
        sa.Column("slo_burn_pct", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "service_id", "window_end",
            name="uq_kg_signal_aggregates_service_window",
        ),
    )
    op.create_index(
        "ix_kg_signal_aggregates_service_window",
        "kg_signal_aggregates",
        ["service_id", sa.text("window_end DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kg_signal_aggregates_service_window",
        table_name="kg_signal_aggregates",
    )
    op.drop_table("kg_signal_aggregates")

    op.drop_index(
        "ix_kg_ingress_obs_ingress_ts",
        table_name="kg_ingress_observations",
    )
    op.drop_table("kg_ingress_observations")

    op.drop_index("ix_kg_cluster_obs_ts", table_name="kg_cluster_observations")
    op.drop_table("kg_cluster_observations")

    op.drop_index("ix_kg_service_health_ts", table_name="kg_service_health")
    op.drop_index(
        "ix_kg_service_health_service_ts",
        table_name="kg_service_health",
    )
    op.drop_table("kg_service_health")
