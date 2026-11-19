"""kg_service_health_hourly: часовые агрегаты метрик сервисов

Revision ID: 20261118_0100
Revises: 20260930_0100
Create Date: 2026-11-18 01:00:00.000000

`kg_service_health` — 2,2 ГБ из 3,8 ГБ базы (замер 18.11.2026: 6,6 млн строк,
хранение 30 дней), при том что сырые десятиминутные точки читают на глубину
семи дней. Новая таблица принимает свёртку точек старше
`KG_HEALTH_RAW_RETENTION_DAYS`: строка на (service_id, час) со средним,
максимумом и числом не-NULL замеров по каждой метрике (`_avg`, `_max`, `_n`)
и общим числом вошедших точек (`samples`). Пишет beat-задача
`kg_health_downsample` (app/knowledge_graph/health_downsample.py), по
умолчанию выключена.

Новая пустая таблица — без блокировок и без backfill. FK на kg_services нет
намеренно: CREATE TABLE с FK взял бы SHARE ROW EXCLUSIVE на kg_services,
которую синки пишут непрерывно (lock_timeout=15s в Job миграций), а у сырья
в проде FK тоже нет. Порядок выката не важен: пока задача выключена, к
таблице никто не обращается. Downgrade безопасен для остальных таблиц, но
удаляет агрегаты — сырьё, из которого они свёрнуты, уже удалено.

Список метрик продублирован литералом, а не импортирован из моделей:
миграция обязана воспроизводить схему на момент своего написания, даже если
`HEALTH_HOURLY_METRICS` потом вырастет (новые колонки — новой миграцией).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261118_0100"
down_revision = "20260930_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_service_health_hourly"
_INDEX = "ix_kg_service_health_hourly_hour"

_METRICS = (
    "cpu_pct", "mem_pct", "restarts_rate", "http_5xx_rate", "p95_latency_ms",
    "orleans_latency_avg_ms", "orleans_timedout_rate",
    "orleans_messaging_fault_rate", "orleans_pings_missed_rate",
    "orleans_rerouted_rate", "orleans_activation_churn",
)


def upgrade() -> None:
    columns = [
        sa.Column("service_id", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("hour", sa.DateTime(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
    ]
    for metric in _METRICS:
        columns.append(sa.Column(f"{metric}_avg", sa.Float(), nullable=True))
        columns.append(sa.Column(f"{metric}_max", sa.Float(), nullable=True))
        # Число не-NULL замеров метрики за час — вес среднего при слиянии.
        columns.append(sa.Column(f"{metric}_n", sa.SmallInteger(), nullable=True))
    op.create_table(
        _TABLE,
        *columns,
        sa.PrimaryKeyConstraint("service_id", "hour", name="pk_kg_service_health_hourly"),
    )
    # BRIN по времени — под ретеншен агрегатов (`hour < cutoff`): строки
    # приходят в порядке времени, индекс занимает килобайты. Таблица пустая,
    # CONCURRENTLY не нужен. SQLite `postgresql_using` игнорирует.
    op.create_index(_INDEX, _TABLE, ["hour"], postgresql_using="brin")


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
