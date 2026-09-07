"""kg_incidents.noise: инцидент из шумовых алертов помечен, а не скрыт

Revision ID: 20260907_0100
Revises: 20260906_0100
Create Date: 2026-09-07 01:00:00.000000

Первые минуты после релиза 1.0.7: 7 из 13 инцидентов —
KubeDeploymentGenerationMismatch, который обогащение давно классифицирует
как шум (`gen_mismatch_noise`: observedGeneration отстаёт при здоровых
репликах). За неделю таких алертов 327 из ~390, каждый на своём сервисе, —
без пометки таблица инцидентов стала бы таблицей этого одного алерта.

Решение о шуме уже принимается в /enrich-and-forward (gen-mismatch, meta,
rollout). Инцидент его наследует: `noise=true`, когда ВСЕ его алерты
шумовые; новый нешумовой алерт снимает флаг. Списки API по умолчанию шум
скрывают, но строка остаётся — инцидент случился, и timeline по нему
доступен.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260907_0100"
down_revision = "20260906_0100"
branch_labels = None
depends_on = None

_TABLE = "kg_incidents"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("noise", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_kg_incidents_noise", _TABLE, ["noise"])


def downgrade() -> None:
    op.drop_index("ix_kg_incidents_noise", table_name=_TABLE)
    op.drop_column(_TABLE, "noise")
