"""add incidents table with trace column

Revision ID: 20260511_0000
Revises: 20240101_0000
Create Date: 2026-05-11 11:00:00.000000

The `incidents` table was previously created at runtime via
`Base.metadata.create_all(engine)` from `app/database.py` (which uses its
own `declarative_base()` separate from the one in `app/models.py`).
This left `incidents` unmanaged by Alembic, so a forward-compatible add of
the per-stage `trace` column couldn't be expressed as a migration.

This revision brings `incidents` under Alembic management with the full
columnset including the new `trace` JSON column populated by the
pipeline tracing infrastructure (`app.core.tracing`).

Migration path for an environment that already has an `incidents` table
from a pre-Alembic `create_all()` run:

    psql -c 'ALTER TABLE incidents ADD COLUMN IF NOT EXISTS trace JSON;'
    alembic stamp 20260511_0000

For a fresh deployment the standard `alembic upgrade head` is enough.
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260511_0000"
down_revision: Union[str, None] = "20240101_0000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True, index=True, nullable=False),
        sa.Column("incident_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("analysis", sa.JSON(), nullable=True),
        sa.Column("trace", sa.JSON(), nullable=True),
        sa.Column("user_feedback", sa.JSON(), nullable=True),
        sa.Column("is_accepted", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("incidents")
