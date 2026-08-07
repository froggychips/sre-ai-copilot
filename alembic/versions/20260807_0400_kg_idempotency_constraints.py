"""kg idempotency constraints: дедуп-ключи против тихой эрозии графа

Revision ID: 20260807_0200
Revises: 20260807_0100
Create Date: 2026-08-07 02:00:00.000000

Три схемо-фикса для идемпотентности KG-синков:

1. kg_deployments: UNIQUE(service_id, buildtype_id, build_number).
   record_deployment дедупил check-then-insert-ом без констрейнта —
   конкурентные вызовы (beat task + incident pipeline) плодили дубли,
   раздувая deploy_count / deploy_failure_pct. Существующие дубли
   схлопываются (остаётся строка с min id) ДО создания констрейнта.
   NULL-ы buildtype/build_number в PG различны — деплои без build-инфо
   констрейнт не ограничивает (это разные события).

2. kg_log_observations: колонка app_name (NOT NULL) + UNIQUE(ts, level,
   source, app_name) вместо UNIQUE(service_id, ts, level, source).
   service_id NULLABLE, NULL-ы в PG различны — для несматченных сервисов
   ON CONFLICT не срабатывал вовсе (каждый retry/tick вставлял дубли).
   Legacy-строки бэкфиллятся детерминированными суррогатами (данные не
   теряются): matched -> legacy-svc:<service_id> (старый констрейнт
   гарантирует уникальность в группе), unmatched -> legacy:<id>.

3. kg_service_edges: колонка direction (NOT NULL, default '') +
   UNIQUE(src_id, dst_id, kind, direction) вместо UNIQUE(src_id, dst_id,
   kind). pub и sub одного NATS-subject теперь сосуществуют как два ребра
   вместо flip-flop-а extras.direction между тиками. direction для
   uses_nats бэкфиллится из extras. Дедуп перед констрейнтом не нужен:
   старый UNIQUE гарантировал не более одной строки на (src, dst, kind).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260807_0400"
down_revision = "20260807_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. kg_deployments: дедуп + UNIQUE ──────────────────────────────
    # Схлопываем дубли (service_id, buildtype_id, build_number) — остаётся
    # строка с минимальным id. Только пары с непустыми build-полями.
    op.execute(
        "DELETE FROM kg_deployments d "
        "USING kg_deployments keep "
        "WHERE d.service_id = keep.service_id "
        "  AND d.buildtype_id = keep.buildtype_id "
        "  AND d.build_number = keep.build_number "
        "  AND d.buildtype_id IS NOT NULL "
        "  AND d.build_number IS NOT NULL "
        "  AND d.id > keep.id"
    )
    op.create_unique_constraint(
        "uq_kg_deploy_service_build",
        "kg_deployments",
        ["service_id", "buildtype_id", "build_number"],
    )

    # ── 2. kg_log_observations: app_name + новый UNIQUE ────────────────
    op.add_column(
        "kg_log_observations",
        sa.Column("app_name", sa.String(), nullable=False, server_default=""),
    )
    # Бэкфилл суррогатов для legacy-строк — без потери данных.
    # Matched: старый констрейнт гарантировал <=1 строку на
    # (service_id, ts, level, source) с непустым service_id, значит
    # суррогат по service_id уникален внутри новой группы.
    op.execute(
        "UPDATE kg_log_observations "
        "SET app_name = 'legacy-svc:' || service_id::text "
        "WHERE service_id IS NOT NULL"
    )
    # Unmatched (это и есть накопленные дубли): суррогат по id — уникален
    # per-строку, констрейнт применится без удаления истории.
    op.execute(
        "UPDATE kg_log_observations "
        "SET app_name = 'legacy:' || id::text "
        "WHERE service_id IS NULL"
    )
    op.drop_constraint(
        "uq_kg_log_obs_service_ts_level_source",
        "kg_log_observations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_kg_log_obs_ts_level_source_app",
        "kg_log_observations",
        ["ts", "level", "source", "app_name"],
    )

    # ── 3. kg_service_edges: direction + новый UNIQUE ──────────────────
    op.add_column(
        "kg_service_edges",
        sa.Column("direction", sa.String(), nullable=False, server_default=""),
    )
    # Legacy uses_nats: направление жило в extras — переносим в колонку.
    # extras имеет тип json: оператор ->> работает и для json, и для jsonb.
    op.execute(
        "UPDATE kg_service_edges "
        "SET direction = COALESCE(extras ->> 'direction', '') "
        "WHERE kind = 'uses_nats'"
    )
    op.drop_constraint(
        "uq_kg_edge_src_dst_kind", "kg_service_edges", type_="unique",
    )
    op.create_unique_constraint(
        "uq_kg_edge_src_dst_kind_direction",
        "kg_service_edges",
        ["src_id", "dst_id", "kind", "direction"],
    )


def downgrade() -> None:
    # ── 3. kg_service_edges ────────────────────────────────────────────
    op.drop_constraint(
        "uq_kg_edge_src_dst_kind_direction", "kg_service_edges", type_="unique",
    )
    # После фикса pub+sub могли разъехаться в два ребра — схлопываем
    # обратно (остаётся min id), иначе старый UNIQUE не применится.
    op.execute(
        "DELETE FROM kg_service_edges e "
        "USING kg_service_edges keep "
        "WHERE e.src_id = keep.src_id "
        "  AND e.dst_id = keep.dst_id "
        "  AND e.kind = keep.kind "
        "  AND e.id > keep.id"
    )
    op.create_unique_constraint(
        "uq_kg_edge_src_dst_kind",
        "kg_service_edges",
        ["src_id", "dst_id", "kind"],
    )
    op.drop_column("kg_service_edges", "direction")

    # ── 2. kg_log_observations ─────────────────────────────────────────
    op.drop_constraint(
        "uq_kg_log_obs_ts_level_source_app",
        "kg_log_observations",
        type_="unique",
    )
    # Возврат старого ключа: дедупим только группы с непустым service_id
    # (NULL-ы в старом констрейнте различны и не конфликтуют). Равенство
    # source — строгое (NULL-source строки не сравниваются, как и в
    # семантике UNIQUE).
    op.execute(
        "DELETE FROM kg_log_observations l "
        "USING kg_log_observations keep "
        "WHERE l.service_id = keep.service_id "
        "  AND l.ts = keep.ts "
        "  AND l.level = keep.level "
        "  AND l.source = keep.source "
        "  AND l.service_id IS NOT NULL "
        "  AND l.id > keep.id"
    )
    op.create_unique_constraint(
        "uq_kg_log_obs_service_ts_level_source",
        "kg_log_observations",
        ["service_id", "ts", "level", "source"],
    )
    op.drop_column("kg_log_observations", "app_name")

    # ── 1. kg_deployments ──────────────────────────────────────────────
    op.drop_constraint(
        "uq_kg_deploy_service_build", "kg_deployments", type_="unique",
    )
