"""add kg_action_approvals (Discord approve/decline persistence)

Revision ID: 20260522_0400
Revises: 20260522_0300
Create Date: 2026-05-22 04:00:00.000000

Persistent storage для approve/decline решений по proposed actions из
Discord-incident-embed'ов. Существующий ApprovalManager (Redis-Lua) хранит
PENDING→APPROVED/REJECTED state ephemeral (TTL 30 мин), но для аудита и
защиты от повторного клика после истечения TTL нужна durable-таблица.

Поток:
  1. Пайплайн шлёт incident-embed с buttons (через bot API).
  2. Пользователь жмёт Approve → handler пишет row {incident_id, intent_signature,
     status=approved, approved_by=<discord_user>, decided_at=now}.
  3. Повторный клик / клик другого пользователя → UNIQUE collision → handler
     отвечает "already {status} by @user at HH:MM".

`intent_signature` — детерминированный хэш ExecutionIntent (action+resource+ns),
не sequence. Это позволяет идемпотентно обрабатывать одну и ту же команду
даже после edit-message / повторной публикации embed-а.

`status` хранит финальное состояние; PENDING-промежутка нет, потому что
кнопка либо нажата (запись создаётся), либо нет (записи не существует).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260522_0400"
down_revision = "20260522_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_action_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("incident_id", sa.String(), nullable=False, index=True),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("intent_signature", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "incident_id", "intent_signature",
            name="uq_kg_action_approvals_incident_intent",
        ),
    )
    op.create_index(
        "ix_kg_action_approvals_status_decided",
        "kg_action_approvals",
        ["status", sa.text("decided_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kg_action_approvals_status_decided",
        table_name="kg_action_approvals",
    )
    op.drop_table("kg_action_approvals")
