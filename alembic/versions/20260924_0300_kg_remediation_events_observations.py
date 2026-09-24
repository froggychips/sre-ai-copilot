"""kg_remediation_events.observations: наблюдения внешнего исполнителя в графе

Revision ID: 20260924_0300
Revises: 20260924_0100
Create Date: 2026-09-24 03:00:00.000000

Медик видит на стенде то, чего граф не сохраняет (dirty/phantom-версия
schema_migrations, отсутствующие ключи Secret, коды выхода), но до сих пор
это лежало только прозой в summary/applied рядом с его выводами. Колонка
хранит структурированные наблюдения (`medic_obs/v1`, без root_cause и
next_action), и сборщик контекста инцидента из графа читает их как ещё один
источник рядом с kg_pod_events и kg_k8s_jobs.

Одна nullable JSON-колонка — ALTER без перезаписи таблицы (~700 строк), без
backfill: у старых событий наблюдения извлекаются тем же экстрактором на
чтении (app/context/medic_observations.observations_of).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0300"
down_revision = "20260924_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kg_remediation_events", sa.Column("observations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("kg_remediation_events", "observations")
