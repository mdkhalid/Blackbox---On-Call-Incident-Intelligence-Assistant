"""force row-level security so the table owner cannot bypass it

Revision ID: 0006_rls_force
Revises: 0005_regions
Create Date: 2026-11-14
"""
from __future__ import annotations

from alembic import op

revision = "0006_rls_force"
down_revision = "0005_regions"
branch_labels = None
depends_on = None

_TABLES = ("conversations", "conversation_turns", "tenant_regions")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    # ENABLE ROW LEVEL SECURITY does not apply to the table owner. The application
    # role is often the owner (or a superuser), which made the 0002 policies inert.
    # FORCE closes that gap for the owner; superuser/BYPASSRLS roles still bypass it,
    # so the deployment must run the app as a non-superuser role.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    if not _is_postgres():
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
