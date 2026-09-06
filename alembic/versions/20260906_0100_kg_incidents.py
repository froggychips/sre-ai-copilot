"""kg_incidents: инцидент как объект графа

Revision ID: 20260906_0100
Revises: 20260905_0300
Create Date: 2026-09-06 01:00:00.000000

До этой миграции инцидента в графе не было: `kg_alerts.incident_id` во всех
3766 строках (замер 06.09.2026) равнялся `fingerprint`, то есть «инцидент»
был другим именем одного алерта, а таблица `incidents` LLM-пайплайна стояла
пустой. Триаж же думает событием сервиса, к которому относятся все его
алерты за окно, деплой перед ним, события подов, аномалии и ошибки логов.

`kg_incidents` — этот объект. Инвариант «не больше одного открытого
инцидента на (namespace, service_name)» держит частичный уникальный индекс:
две реплики API на одном алерт-шторме иначе завели бы два инцидента, и
проверка в коде их не поймала бы.

`kg_alerts.incident_id` с этого момента получает `incident_key`
(`ns/service@время`) вместо копии fingerprint. Колонка и индекс на ней уже
есть, менять их не нужно; старые строки со значением fingerprint остаются —
timeline ищет алерты инцидента по списку `fingerprints`, а не по этой
колонке, поэтому обратная засыпка не требуется.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_0100"
down_revision = "20260905_0300"
branch_labels = None
depends_on = None

_TABLE = "kg_incidents"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_key", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("service_name", sa.String(), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("kg_services.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("last_alert_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolve_reason", sa.String(), nullable=True),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alertnames", sa.JSON(), nullable=True),
        sa.Column("fingerprints", sa.JSON(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_kg_incidents_incident_key", _TABLE, ["incident_key"], unique=True)
    op.create_index("ix_kg_incidents_service_id", _TABLE, ["service_id"])
    op.create_index("ix_kg_incidents_opened_at", _TABLE, ["opened_at"])
    op.create_index(
        "ix_kg_incidents_ns_service_status", _TABLE, ["namespace", "service_name", "status"],
    )
    op.create_index(
        "uq_kg_incidents_one_open_per_service", _TABLE, ["namespace", "service_name"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
