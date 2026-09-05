"""kg_services: идентичность объекта, а не его имени

Revision ID: 20260905_0200
Revises: 20260905_0100
Create Date: 2026-09-05 02:00:00.000000

Узел графа опознавался тройкой (namespace, name, node_kind), и этого хватало
ровно до первого пересоздания. Снесённый и заведённый заново Deployment —
другой объект k8s с другим `uid`, но для графа он неотличим от прежнего: к
нему прирастает вся старая история — деплои, алерты, health, рёбра.

У namespace эта проблема решена 14.08.2026 (`kg_namespaces.k8s_uid` +
`incarnation` + `namespace_lifecycle`), у workload'ов — нет, хотя
пересоздают их на порядок чаще: каждый `--wipe` сквада, каждая смена
селектора, каждый helm uninstall/install.

Три колонки:

  * `k8s_uid` — идентичность объекта. NULL = «источник не сообщил uid», а не
    «объекта нет»: узлы из алертов и ingress-синтетики заводятся без обхода
    k8s API, и таких на 05.09.2026 большинство;
  * `incarnation` — растёт, когда под тем же именем появился объект с другим
    uid. Дефолт 1 для всех существующих строк: до этой миграции инкарнацию
    никто не считал, и объявлять накопленное «первым воплощением» — самое
    честное, что можно сказать о данных, которых нет;
  * `incarnation_changed_at` — когда сменилась. Без метки факт пересоздания
    виден только как «число стало 2», без ответа когда — а значит и без
    возможности связать его с инцидентом.

Индексов не добавляем намеренно. `k8s_uid` читается через уже существующий
уникальный ключ (namespace, name, node_kind) — запросов «найди по uid» в
коде нет, а лишний индекс на kg_services блокируется каждым ALTER TABLE и
обновляется на каждом upsert'е (17 938 узлов за прогон синка).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0200"
down_revision = "20260905_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_services"
_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() == "postgresql":
        # ADD COLUMN с константным DEFAULT в PG 11+ не переписывает таблицу,
        # но ACCESS EXCLUSIVE берёт. Ограничиваем ожидание, чтобы висящий
        # читатель не утащил миграцию за собой (POSTMORTEM 2026-08-08 §3.2).
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))

    op.add_column(_TABLE, sa.Column("k8s_uid", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column(
        "incarnation", sa.Integer(), nullable=False, server_default="1",
    ))
    op.add_column(_TABLE, sa.Column(
        "incarnation_changed_at", sa.DateTime(), nullable=True,
    ))


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.drop_column(_TABLE, "incarnation_changed_at")
    op.drop_column(_TABLE, "incarnation")
    op.drop_column(_TABLE, "k8s_uid")
