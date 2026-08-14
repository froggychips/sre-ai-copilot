"""kg_namespaces — namespace как объект со своей жизнью

Revision ID: 20260814_0200
Revises: 20260814_0100
Create Date: 2026-08-14 02:00:00.000000

До этой таблицы граф знал про namespace только имя — строку в kg_services.
Отсюда три поломки, замеренные на проде 14.08.2026:

  * выход не отслеживался: 198 namespace в графе против 139 живых, узлы
    снесённых стендов оставались навсегда;
  * пересоздание было невидимо: `squad-1-shared` имеет узлы на 82 дня старше
    самого namespace, к ним прилипло 39 775 health-точек прошлой инкарнации;
  * уборка блокировала сама себя: guard `drift_pct > 20%` при фактических
    29.8% переставал работать ровно тогда, когда мусора больше всего.

Таблица только заводится — заполнение и переходы состояний приедут
следующими шагами. Это намеренно: сначала наблюдение, потом действие.

Backfill: строки создаются из уже известных graph-namespace со state=active
и incarnation=1. UID при этом NULL — его проставит первый синк, который
прочитает namespace из кластера. NULL здесь честнее выдумки: для
существующих строк инкарнация действительно неизвестна.

⚠️ CREATE TABLE не берёт ACCESS EXCLUSIVE на существующие таблицы, поэтому
блокировок читателей тут нет. lock_timeout всё равно выставляем — INSERT
backfill'а идёт по kg_services, и висящий писатель не должен утащить его за
собой (POSTMORTEM §3.2).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260814_0200"
down_revision = "20260814_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_namespaces"
_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def _set_lock_timeout() -> None:
    if _dialect() != "postgresql":
        return
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    _set_lock_timeout()
    if _TABLE in _tables():
        return

    op.create_table(
        _TABLE,
        sa.Column("namespace", sa.String(), primary_key=True),
        sa.Column("k8s_uid", sa.String(), nullable=True),
        sa.Column("k8s_created_at", sa.DateTime(), nullable=True),
        sa.Column("incarnation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("missing_since", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_kg_namespaces_state", _TABLE, ["state"])
    op.create_index("ix_kg_namespaces_k8s_uid", _TABLE, ["k8s_uid"])
    op.create_index("ix_kg_namespaces_last_seen_at", _TABLE, ["last_seen_at"])
    op.create_index("ix_kg_namespaces_missing_since", _TABLE, ["missing_since"])

    # Backfill из того, что граф уже знает. state=active намеренно: пока синк
    # не сверился с кластером, объявлять namespace исчезнувшим не на чем.
    now_fn = "now()" if _dialect() == "postgresql" else "CURRENT_TIMESTAMP"
    op.execute(sa.text(
        f"INSERT INTO {_TABLE} "
        "(namespace, incarnation, state, first_seen_at, last_seen_at, updated_at) "
        f"SELECT DISTINCT namespace, 1, 'active', {now_fn}, {now_fn}, {now_fn} "
        "FROM kg_services WHERE namespace IS NOT NULL"
    ))


def downgrade() -> None:
    _set_lock_timeout()
    if _TABLE not in _tables():
        return
    op.drop_table(_TABLE)
