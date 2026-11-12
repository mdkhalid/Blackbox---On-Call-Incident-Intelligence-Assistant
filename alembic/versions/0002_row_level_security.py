"""row-level security for tenant isolation (Postgres only)

Revision ID: 0002_rls
Revises: 0001_initial
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op

revision = "0002_rls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    # Defense in depth: even a query that forgets the tenant predicate cannot read
    # another tenant's rows. The application still sets `app.tenant_id` per request;
    # RLS is the backstop, not the primary control.
    op.execute("ALTER TABLE conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_turns ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY conversations_tenant_isolation ON conversations
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
        """
    )
    op.execute(
        """
        CREATE POLICY conversation_turns_tenant_isolation ON conversation_turns
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP POLICY IF EXISTS conversation_turns_tenant_isolation ON conversation_turns")
    op.execute("DROP POLICY IF EXISTS conversations_tenant_isolation ON conversations")
    op.execute("ALTER TABLE conversation_turns DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversations DISABLE ROW LEVEL SECURITY")