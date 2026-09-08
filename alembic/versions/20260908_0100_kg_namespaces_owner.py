"""kg_namespaces: владелец стенда как факт графа

Revision ID: 20260908_0100
Revises: 20260907_0300
Create Date: 2026-09-08 01:00:00.000000

До этой миграции «кто владеет squad-N» отвечали три несогласованных места:
статическая карта TC-логин → Discord у squad-medic (15 записей, из 19
текущих деплойеров нет 9 — 4 из 6 больных сквадов 08.09.2026 пинговались
«владелец не определён»), `scripts/squad_dashboard.py` (лейбл deployed-by +
TeamCity + Jira) и скилл squad-occupancy в vibecode (assignee Jira по ветке).
Ни один не читал другого.

Теперь владелец — колонки на `kg_namespaces`, их заполняет beat-задача
`kg_namespace_owner_sync`; медик, дашборд и MCP-тул `kg_squad_owners`
читают отсюда. Сырые лейблы `deployed_by` / `deployed_branch` пишет
lifecycle на каждом тике (у него уже есть `kubectl get ns -o json`),
резолв (`owner_*`) — раз в час. `last_activity_at` — последняя игровая
сессия по ClickHouse сквада (опционально, для критерия «стенд простаивает»
у медика вместо снятого stale-гейта).

ADD COLUMN nullable без DEFAULT — метаданные; таблица маленькая (~200
строк), писатель один (lifecycle, */10), lock_timeout=15s достаточно.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260908_0100"
down_revision = "20260907_0300"
branch_labels = None
depends_on = None

_TABLE = "kg_namespaces"
_LOCK_TIMEOUT = "15s"

_COLUMNS = (
    # сырые лейблы namespace — пишет lifecycle каждым тиком
    sa.Column("deployed_by", sa.String(), nullable=True),
    sa.Column("deployed_branch", sa.String(), nullable=True),
    # резолв владельца — пишет kg_namespace_owner_sync
    sa.Column("owner_login", sa.String(), nullable=True),
    sa.Column("owner_source", sa.String(), nullable=True),
    sa.Column("owner_jira_key", sa.String(), nullable=True),
    sa.Column("owner_discord_id", sa.String(), nullable=True),
    sa.Column("owner_resolved_at", sa.DateTime(), nullable=True),
    # последняя игровая активность по ClickHouse сквада (опционально)
    sa.Column("last_activity_at", sa.DateTime(), nullable=True),
)


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    for col in _COLUMNS:
        op.add_column(_TABLE, col)
    op.create_index("ix_kg_namespaces_owner_login", _TABLE, ["owner_login"])


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.drop_index("ix_kg_namespaces_owner_login", table_name=_TABLE)
    for col in reversed(_COLUMNS):
        op.drop_column(_TABLE, col.name)
