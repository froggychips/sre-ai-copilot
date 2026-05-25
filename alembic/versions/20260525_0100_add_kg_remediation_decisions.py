"""kg_remediation_decisions — Phase A foundation (preview only, no executor).

Revision ID: 20260525_0100
Revises: 20260524_0200
Create Date: 2026-05-25 01:00:00.000000

Phase A scope (см. memory/project_remediation_pipeline_plan.md): одна table
для audit `RemediationDecisionPreview`. Триплет actions/observations/
approvals будет создан в Phase B+ когда появится executor.

UNIQUE (incident_id, idempotency_key) — повторное срабатывание того же
alert+playbook не плодит дубли decision rows.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260525_0100"
down_revision = "20260524_0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_remediation_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.String(), nullable=True),
        sa.Column("alert_fingerprint", sa.String(), nullable=True),
        sa.Column("target_ref", sa.JSON(), nullable=True),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("classification_provenance", sa.JSON(), nullable=True),
        sa.Column("risk_axes", sa.JSON(), nullable=True),
        sa.Column("candidate_playbooks", sa.JSON(), nullable=True),
        sa.Column("selected_playbook", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("decision_reasons", sa.JSON(), nullable=True),
        sa.Column("command_preview", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "incident_id", "idempotency_key",
            name="uq_kg_remediation_decisions_incident_idem",
        ),
    )
    op.create_index(
        "ix_kg_remediation_decisions_incident_id",
        "kg_remediation_decisions", ["incident_id"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_alert_fingerprint",
        "kg_remediation_decisions", ["alert_fingerprint"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_classification",
        "kg_remediation_decisions", ["classification"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_selected_playbook",
        "kg_remediation_decisions", ["selected_playbook"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_decision",
        "kg_remediation_decisions", ["decision"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_idempotency_key",
        "kg_remediation_decisions", ["idempotency_key"],
    )
    op.create_index(
        "ix_kg_remediation_decisions_incident_idem",
        "kg_remediation_decisions",
        ["incident_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kg_remediation_decisions_incident_idem",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_idempotency_key",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_decision",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_selected_playbook",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_classification",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_alert_fingerprint",
        table_name="kg_remediation_decisions",
    )
    op.drop_index(
        "ix_kg_remediation_decisions_incident_id",
        table_name="kg_remediation_decisions",
    )
    op.drop_table("kg_remediation_decisions")
