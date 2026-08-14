"""kg_services.owner_source — откуда взялся team_owner

Revision ID: 20260814_0100
Revises: 20260808_0200
Create Date: 2026-08-14 01:00:00.000000

`team_owner` заполнен у 12 577 узлов из 13 074, но по строке невозможно
сказать, откуда он взялся: проставлен вручную, выведен из k8s-лейбла,
угадан по префиксу namespace или унаследован от истории деплоев. Разные
источники заслуживают разного доверия — префиксная эвристика ошибается на
переименованиях, лейбл не врёт, — а сейчас все они выглядят одинаково.

Колонка nullable без backfill намеренно: у существующих строк источник
неизвестен, и любое значение по умолчанию было бы выдумкой. NULL читается
как «провенанс неизвестен» — честнее, чем задним числом объявить всё
`labels`. Заполняться будет естественным ходом: каждый синк, переписывающий
team_owner, проставит и свой источник.

⚠️ `ALTER TABLE kg_services ADD COLUMN` требует ACCESS EXCLUSIVE — ровно та
операция, которая 08.08.2026 повесила прод на 6 минут (POSTMORTEM §3.2):
воркеры держали долгие транзакции, DDL встал в очередь, а ожидающий DDL
блокирует всех, кто пришёл ПОСЛЕ него. Job миграций уже ходит с
`PGOPTIONS=-c lock_timeout=15s` (k8s/migrate-job.yaml), здесь выставляем
`SET LOCAL` дополнительно: миграцию запускают и вручную из пода, где
PGOPTIONS может не быть.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260814_0100"
down_revision = "20260808_0200"
branch_labels = None
depends_on = None

_TABLE = "kg_services"
_COLUMN = "owner_source"
# 15s — тот же порядок, что в migrate-job: не получить лок за это время
# безвреднее, чем утащить за собой читателей.
_LOCK_TIMEOUT = "15s"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def _set_lock_timeout() -> None:
    """SET LOCAL lock_timeout — только на Postgres, только внутри транзакции."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def upgrade() -> None:
    _set_lock_timeout()
    # Идемпотентно: колонка могла появиться через Base.metadata.create_all()
    # в обход alembic (см. 20260807_0300).
    if _COLUMN not in _existing_columns():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    _set_lock_timeout()
    if _COLUMN not in _existing_columns():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
