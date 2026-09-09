"""kg_namespaces: кто занял стенд кнопкой (лейбл squad-owner)

Revision ID: 20260909_0100
Revises: 20260908_0200
Create Date: 2026-09-09 01:00:00.000000

Резолв владельца (20260908_0100) знал только `deployed-by` — лейбл того, кто
последним катал полный деплой. Он остаётся от прежнего хозяина стенда, пока
новый не задеплоит сам, поэтому 09.09.2026 два сквада две недели числились за
человеком, который их освободил (жалоба звучала как «дашборд не обновляется»,
хотя доска обновлялась исправно — врала атрибуция).

Кнопка «Сквад-окружение: занять / освободить» при этом пишет настоящего
владельца прямо на namespace — лейбл `squad-owner` (плюс `squad-claim-build` и
аннотация `squad-claimed-at`). На 09.09.2026 он стоял на 40 из 46 стендов и в
шести случаях расходился с `deployed-by`. Читать этот факт дешевле и надёжнее,
чем восстанавливать его сканом истории TeamCity: лейбл живёт вместе со стендом
и не зависит ни от окна поиска, ни от доступности TC.

ADD COLUMN nullable без DEFAULT — метаданные; таблица маленькая (~200 строк),
писатель один (lifecycle, */10), lock_timeout=15s достаточно.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260909_0100"
down_revision = "20260908_0200"
branch_labels = None
depends_on = None

_TABLE = "kg_namespaces"
_LOCK_TIMEOUT = "15s"

#: Сырой лейбл `squad-owner` — пишет lifecycle каждым тиком, рядом с
#: `deployed_by`/`deployed_branch`.
_COLUMN = sa.Column("claim_owner", sa.String(), nullable=True)


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.add_column(_TABLE, _COLUMN)


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.drop_column(_TABLE, _COLUMN.name)
