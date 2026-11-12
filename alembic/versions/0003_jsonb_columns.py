"""convert JSON columns to JSONB on Postgres

Revision ID: 0003_jsonb
Revises: 0002_rls
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op

revision = "0003_jsonb"
down_revision = "0002_rls"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    for table, column in (
        ("conversations", "memory_json"),
        ("conversations", "last_budget_json"),
        ("conversation_turns", "trace_json"),
        ("conversation_turns", "budget_json"),
        ("conversation_turns", "chunks_json"),
    ):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE jsonb USING {column}::jsonb"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table, column in (
        ("conversations", "memory_json"),
        ("conversations", "last_budget_json"),
        ("conversation_turns", "trace_json"),
        ("conversation_turns", "budget_json"),
        ("conversation_turns", "chunks_json"),
    ):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE json USING {column}::json"
        )