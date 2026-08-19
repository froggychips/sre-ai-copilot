"""incidents: состояние доставки и исполнения — в колонки

Revision ID: 20260819_0200
Revises: 20260819_0100
Create Date: 2026-08-19 02:00:00.000000

В `analysis` жила машина состояний из девяти ключей, и среди них два
координационных примитива:

  * `report_pending` / `report_sent` / `report_failed` — outbox доставки
    отчёта: по нему решают, кому досылать;
  * `executor_in_flight` — claim исполнителя с TTL: по нему решают, не
    выполняется ли действие прямо сейчас в соседнем воркере.

Координация в JSON плоха тремя вещами сразу. Поиск «кому досылать» — полный
скан (GIN добавили миграцией 20260819_0100, но индекс по наличию ключа всё
равно грубее колонки). Состояния не видно в схеме: человек, открывший
таблицу, видит «поле с результатом разбора». И read-modify-write по JSON
требует внешнего row-lock там, где колонка обошлась бы обычным UPDATE.

Эта миграция выносит СОСТОЯНИЕ, оставляя ДАННЫЕ на месте: payload отчёта
(поля embed) и результат исполнения — это по-прежнему JSON, и там ему место.
Разделение проходит по линии «по чему ищут и координируются» против «что
показываем».

Колонки nullable намеренно: NULL = «этой стадии не было», что отличается от
«была и завершилась». Такое различение JSON давал наличием ключа, и терять
его при переезде не хочется.

Таблица пуста (пайплайн под флагом LLM_PIPELINE_ENABLED), поэтому backfill не
нужен — но код всё равно умеет читать старые JSON-ключи, чтобы миграция и
выкат не были связаны порядком.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260819_0200"
down_revision = "20260819_0100"
branch_labels = None
depends_on = None

_TABLE = "incidents"
_LOCK_TIMEOUT = "15s"

_COLUMNS = (
    # Доставка отчёта: pending | sent | failed. NULL — отчёт не готовился.
    ("report_state", sa.String()),
    ("report_attempts", sa.Integer()),
    ("report_updated_at", sa.DateTime()),
    # Исполнение: in_flight | applied | state_unknown | disabled.
    ("executor_state", sa.String()),
    # Момент взятия claim'а — по нему считается TTL вместо разбора JSON.
    ("executor_claimed_at", sa.DateTime()),
)


def _dialect() -> str:
    return op.get_bind().dialect.name


def _set_lock_timeout() -> None:
    if _dialect() != "postgresql":
        return
    # ADD COLUMN без DEFAULT — это лишь запись в каталог, но висящий читатель
    # всё равно не должен утащить миграцию (POSTMORTEM 2026-08-08 §3.2).
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _existing() -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    _set_lock_timeout()
    have = _existing()
    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))

    if _dialect() != "postgresql":
        return
    # Частичные индексы: строк с непустым состоянием мало, и именно их ищут.
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS ix_incidents_report_state "
        f"ON {_TABLE} (report_state) WHERE report_state IS NOT NULL"
    ))
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS ix_incidents_executor_state "
        f"ON {_TABLE} (executor_state) WHERE executor_state IS NOT NULL"
    ))


def downgrade() -> None:
    _set_lock_timeout()
    if _dialect() == "postgresql":
        for name in ("ix_incidents_executor_state", "ix_incidents_report_state"):
            op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    have = _existing()
    for name, _ in reversed(_COLUMNS):
        if name in have:
            op.drop_column(_TABLE, name)
