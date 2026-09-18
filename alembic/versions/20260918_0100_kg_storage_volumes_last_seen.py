"""kg_storage_volumes.last_seen_at: «синк видел этот том», отдельно от updated_at

Revision ID: 20260918_0100
Revises: 20260917_0100
Create Date: 2026-09-18 01:00:00.000000

`updated_at` отвечает на вопрос «когда строка менялась», а чистке узлов
нужен другой: «когда синк её видел». Разница не теоретическая: ORM не
эмитит UPDATE, если все поля совпали с прежними, а у PV поля не меняются
годами. Замер 18.09.2026 — из 10 059 PV моложе суток 61 строка, при том
что каждый прогон синка исправно видит 1214 живых. По `updated_at`
«недавно виденных» получается 61, то есть почти ноль, и порог усадки,
который на этом знаменателе стоит, пропускает любой обрезанный снимок.

Колонка nullable: ADD COLUMN без DEFAULT не переписывает таблицу и не
держит блокировку. Backfill из `updated_at` — 12 170 строк, одна команда;
он не делает старые записи «виденными», потому что у мусора `updated_at`
и так из мая.

Читатель всё равно берёт `coalesce(last_seen_at, updated_at)`: строка,
вставленная между этой миграцией и первым прогоном нового кода, ещё не
отмечена, и считать её невиданной было бы неправдой.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260918_0100"
down_revision = "20260917_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_storage_volumes"
_COLUMN = "last_seen_at"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(), nullable=True))
    # Литералом, не f-строкой: собранный SQL Bandit разбирает как
    # потенциальную инъекцию (B608), и он прав — привычка важнее того,
    # что здесь обе подстановки константы.
    op.execute("UPDATE kg_storage_volumes SET last_seen_at = updated_at")


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
