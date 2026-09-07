"""kg_service_health: здоровье Orleans-силоса — пять колонок

Revision ID: 20260907_0200
Revises: 20260907_0100
Create Date: 2026-09-07 02:00:00.000000

С 27.08.2026 чарт town-grainhost несёт `VMPodScrape` на `/metrics` (порт 8080,
keep-фильтр по `microsoft_orleans_*`), и метер Microsoft.Orleans доезжает до
VictoriaMetrics pull'ом — в 24 namespace на 07.09 (preprod/preupdate/squad),
в prod пока нет. Это первый источник здоровья ПРИЛОЖЕНИЯ для ядра игрового
бэкенда: до сих пор про grainhost граф знал только cpu/mem/restarts.

Пять nullable-колонок — отдельная семья, не подмена `http_5xx_rate` /
`p95_latency_ms` (те ждут WO-12483): латентность здесь средняя по
grain-вызовам (гистограммы в скрейпе нет), остальное — про membership и
активации силоса. ADD COLUMN без DEFAULT в PG — только метаданные, таблица
в 8 млн строк не переписывается; lock_timeout — чтобы висящий читатель не
утащил миграцию за собой.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260907_0200"
down_revision = "20260907_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_service_health"
_LOCK_TIMEOUT = "15s"
_COLUMNS = (
    "orleans_latency_avg_ms",
    "orleans_timedout_rate",
    "orleans_messaging_fault_rate",
    "orleans_pings_missed_rate",
    "orleans_activation_churn",
)


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    for name in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
