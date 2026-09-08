"""kg_remediation_events: действия внешних исполнителей над стендами

Revision ID: 20260908_0200
Revises: 20260908_0100
Create Date: 2026-09-08 02:00:00.000000

`kg_remediation_decisions` знает только про собственный executor копилота.
squad-medic (CronJob в ns mcp) лечит сквады каждые 15 минут и о его
действиях граф не знал: 07.09.2026 на ImagePullBackOff squad-39 медик
применил 13 бесполезных grant-фиксов и через 14 часов запинговал владельца,
копилот в тот же час выложил карточку по тому же стенду, и ни в timeline
инцидента, ни в дайджесте действий медика не было.

Таблица — одно событие на прогон исполнителя по стенду: applied / manual /
gaps / outcome / next_action и ссылка на открытый в тот момент инцидент
графа. Пишется через `POST /webhooks/remediation` (HMAC, fail-closed),
читается timeline'ом и MCP-тулами. Новая таблица — без блокировок.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260908_0200"
down_revision = "20260908_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_remediation_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("namespaces", sa.JSON(), nullable=True),
        sa.Column("squad", sa.String(), nullable=True),
        sa.Column("service_name", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("fixed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("still_unhealthy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("applied", sa.JSON(), nullable=True),
        sa.Column("manual", sa.JSON(), nullable=True),
        sa.Column("gaps", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("owner_login", sa.String(), nullable=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("kg_incidents.id"), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("actor", "run_id", "namespace", name="uq_kg_remediation_events_run"),
    )
    # actor отдельно не индексируем: uq_kg_remediation_events_run покрывает
    # его как префикс (урок 1.0.4 — 1,7 ГБ дублирующих индексов).
    op.create_index("ix_kg_remediation_events_namespace", _TABLE, ["namespace"])
    op.create_index("ix_kg_remediation_events_squad", _TABLE, ["squad"])
    op.create_index("ix_kg_remediation_events_started_at", _TABLE, ["started_at"])
    op.create_index("ix_kg_remediation_events_incident_id", _TABLE, ["incident_id"])


def downgrade() -> None:
    op.drop_table(_TABLE)
