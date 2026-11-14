"""tenant home regions and conversation region attribution

Revision ID: 0005_regions
Revises: 0004_partition
Create Date: 2026-11-14
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_regions"
down_revision = "0004_partition"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("region", sa.String(), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("home_region", sa.String(), nullable=True)
    )
    op.create_index("ix_conversations_region", "conversations", ["region"])

    op.create_table(
        "tenant_regions",
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("home_region", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_tenant_regions_home_region", "tenant_regions", ["home_region"]
    )

    if _is_postgres():
        # The mapping table is tenant data too, so it gets the same backstop as the
        # conversation tables. The application sets `app.tenant_id` per transaction.
        op.execute("ALTER TABLE tenant_regions ENABLE ROW LEVEL SECURITY")
        op.execute(
            """
            CREATE POLICY tenant_regions_tenant_isolation ON tenant_regions
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_regions_tenant_isolation ON tenant_regions"
        )
        op.execute("ALTER TABLE tenant_regions DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tenant_regions_home_region", table_name="tenant_regions")
    op.drop_table("tenant_regions")
    op.drop_index("ix_conversations_region", table_name="conversations")
    op.drop_column("conversations", "home_region")
    op.drop_column("conversations", "region")
