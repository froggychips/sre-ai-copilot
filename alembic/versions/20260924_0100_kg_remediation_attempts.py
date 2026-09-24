"""kg_remediation_attempts: попытки исполнения как строки, а не ключи JSON

Revision ID: 20260924_0100
Revises: 20260918_0100
Create Date: 2026-09-24 01:00:00.000000

Claim, итог и верификация исполнителя жили в `incidents.analysis`
(`executor_in_flight` / `executor_applied` / `executor_verification`), и
защита от второй записи в кластер держалась на row-lock плюс на том, что
каждый писатель analysis мержит блоб, а не заменяет его. Таблица даёт одну
строку на (incident_id, signature) — как одобрение в `kg_action_approvals`:
claim становится вставкой, гонку ловит UNIQUE самой БД, а жизненный цикл
(claimed → applied → verified / verification_failed, failed, unknown)
читается обычным запросом.

Новая пустая таблица — без блокировок и без backfill: записи, сделанные до
неё, исполнитель распознаёт по JSON-ключам (app/services/executor_apply.py).
Downgrade безопасен для apply-пути: код продолжает писать JSON (dual-write),
теряется только история попыток.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0100"
down_revision = "20260918_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_remediation_attempts"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.String(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=True),
        sa.Column("resource_name", sa.String(), nullable=True),
        sa.Column("intent", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("applied_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "incident_id", "signature",
            name="uq_kg_remediation_attempts_incident_signature",
        ),
    )
    op.create_index(
        "ix_kg_remediation_attempts_status_updated",
        _TABLE,
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_remediation_attempts_status_updated", table_name=_TABLE)
    op.drop_table(_TABLE)
