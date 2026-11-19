"""Индексы по времени под ретеншен наблюдательных таблиц

Revision ID: 20261118_0200
Revises: 20261118_0100
Create Date: 2026-11-18 02:00:00.000000

Ретеншен (`health_retention.purge_table`) удаляет батчами
`WHERE <время> < :cutoff ORDER BY <время> LIMIT :n`. У трёх таблиц нет
индекса, начинающегося с колонки времени, и каждый батч читает таблицу или
чужой индекс целиком. EXPLAIN (ANALYZE) на проде 18.11.2026:

    kg_signal_aggregates.window_end   Parallel Seq Scan, 856 тыс. строк (78 МБ)
    kg_ingress_observations.ts        Parallel Seq Scan, 131 тыс. строк (30 МБ)
    kg_anomaly_observations.ts        Bitmap Index Scan по (severity, ts)
                                      целиком — 14 МБ индекса на батч

Все три таблицы только вставляются и удаляются (n_tup_upd = 0), поэтому
новый индекс не отнимает HOT-обновлений; колонка времени повторяется
тысячами (у агрегатов 728 разных значений на 856 тысяч строк), и
дедупликация b-tree делает индексы маленькими.

Что сюда НЕ вошло и почему (те же замеры):

  * kg_log_observations.ts — уже первая колонка
    uq_kg_log_obs_ts_level_source_app, план — Bitmap Index Scan по нему;
  * kg_pod_events.last_seen — колонка обновляется при каждом повторном
    наблюдении события, и 86% этих обновлений сейчас HOT (145 520 из
    169 752); индекс по ней отнял бы HOT у всех ради одного seq scan на
    53 МБ в сутки;
  * kg_alerts.resolved_at, kg_incidents.resolved_at — 6 и 19 тысяч строк,
    seq scan 7 и 16 мс раз в сутки.

CREATE INDEX CONCURRENTLY: обычный CREATE INDEX держит SHARE-лок и
останавливает запись в таблицу на время построения. CONCURRENTLY не работает
внутри транзакции — отсюда autocommit_block. Оборванное построение
(таймаут, рестарт Job) оставляет INVALID-индекс, который `IF NOT EXISTS`
принял бы за готовый, поэтому невалидный остаток сначала снимается.
`lock_timeout` на время построения сбрасывается: CONCURRENTLY ждёт
завершения чужих транзакций, никого при этом не блокируя, а 15 секунд из
PGOPTIONS Job-а оборвали бы его на первом же долгом синке.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261118_0200"
down_revision = "20261118_0100"
branch_labels = None
depends_on = None

#: (индекс, таблица, колонка). Литералы, не ввод: в SQL подставляются текстом.
_INDEXES = (
    ("ix_kg_signal_aggregates_window_end", "kg_signal_aggregates", "window_end"),
    ("ix_kg_anomaly_observations_ts", "kg_anomaly_observations", "ts"),
    ("ix_kg_ingress_observations_ts", "kg_ingress_observations", "ts"),
)


def _dialect() -> str:
    return op.get_bind().dialect.name


def _has_invalid_index(name: str) -> bool:
    """Остаток оборванного CREATE INDEX CONCURRENTLY под этим именем."""
    if op.get_context().as_sql:
        return False        # `--sql`: соединения нет, проверять нечем
    row = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        ),
        {"name": name},
    ).first()
    return row is not None


def upgrade() -> None:
    if _dialect() != "postgresql":
        return          # sqlite в тестах: индексы создаются из моделей
    with op.get_context().autocommit_block():
        op.execute(sa.text("SET lock_timeout = 0"))
        for name, table, column in _INDEXES:
            if _has_invalid_index(name):
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(sa.text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})"
            ))
        op.execute(sa.text("RESET lock_timeout"))


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name, _table, _column in _INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
