"""ingress:*-узлы получают node_kind='ingress'

Revision ID: 20260815_0100
Revises: 20260814_0200
Create Date: 2026-08-15 01:00:00.000000

`NODE_KIND_INGRESS` объявлен в контракте с самого введения поля `node_kind`,
но не проставлялся никогда: `k8s_ingress_sync` звал `upsert_service` без
аргумента, и все узлы уходили в default. Замер 15.08.2026 — 559 узлов
`ingress:*`, все с `node_kind='service'`, при нуле узлов с `'ingress'`.

Смысл поля от этого терялся ровно там, где оно и вводилось: `node_kind`
появился, чтобы узел перестал означать сразу две сущности (k8s Service и
Deployment схлопывались в одну строку). Внешняя точка входа — третья
сущность, и она снова пряталась под `'service'`.

Почему это безопасно:

  * ключ узла — (namespace, name, node_kind), и строк с
    (ns, 'ingress:<host>', 'ingress') ещё нет: конфликта при UPDATE не будет;
  * `id` не меняются, значит 3218 рёбер от этих узлов остаются целыми;
  * метрики качества (orphan / owner coverage) считаются по
    `NOT synthetic`, а все 559 узлов synthetic — цифры не сдвинутся;
  * единственный поиск ingress-узла (`_canonical_host_node_ns`) идёт по имени
    без `node_kind`.

Без этой миграции код после смены `upsert_service` создал бы ВТОРУЮ копию
каждого узла — с тем же именем, но другим node_kind, — и рёбра разъехались бы
между старой и новой.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260815_0100"
down_revision = "20260814_0200"
branch_labels = None
depends_on = None

_LOCK_TIMEOUT = "15s"


def _dialect() -> str:
    return op.get_bind().dialect.name


def _set_lock_timeout() -> None:
    if _dialect() != "postgresql":
        return
    # UPDATE берёт ROW EXCLUSIVE, а не ACCESS EXCLUSIVE, но висящий писатель
    # всё равно не должен утащить миграцию за собой (POSTMORTEM 2026-08-08 §3.2).
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def upgrade() -> None:
    _set_lock_timeout()
    op.execute(sa.text(
        "UPDATE kg_services SET node_kind = 'ingress' "
        "WHERE name LIKE 'ingress:%' AND node_kind = 'service'"
    ))


def downgrade() -> None:
    _set_lock_timeout()
    op.execute(sa.text(
        "UPDATE kg_services SET node_kind = 'service' "
        "WHERE name LIKE 'ingress:%' AND node_kind = 'ingress'"
    ))
