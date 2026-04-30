"""initial

Revision ID: 20240101_0000
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Optional
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20240101_0000'
down_revision: Optional[str] = None
branch_labels: Optional[Sequence[str], None] = None
depends_on: Optional[Sequence[str], None] = None


def upgrade() -> None:
    # Create conversations table
    op.create_table(
        'conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create messages table
    op.create_table(
        'messages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.Enum('user', 'assistant', name='messagerole'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('messages')
    op.drop_table('conversations')
    # Drop enum type manually for postgres
    sa.Enum(name='messagerole').drop(op.get_bind(), checkfirst=False)
