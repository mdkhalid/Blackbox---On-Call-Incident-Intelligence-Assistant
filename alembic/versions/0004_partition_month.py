"""add partition_month to conversation_turns for partition pruning

Revision ID: 0004_partition
Revises: 0003_jsonb
Create Date: 2026-11-12
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_partition"
down_revision = "0003_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_turns",
        sa.Column("partition_month", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_conversation_turns_partition_month",
        "conversation_turns",
        ["partition_month"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_turns_partition_month", table_name="conversation_turns")
    op.drop_column("conversation_turns", "partition_month")