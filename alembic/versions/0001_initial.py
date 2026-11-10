"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-10
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="default"),
        sa.Column("memory_json", sa.JSON(), nullable=False),
        sa.Column("last_budget_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="default"),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("assistant_message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trace_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("chunks_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_conversation_turns_conversation_id",
        "conversation_turns",
        ["conversation_id"],
    )
    op.create_index("ix_conversation_turns_tenant_id", "conversation_turns", ["tenant_id"])

    op.create_table(
        "services",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.create_table(
        "error_codes",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.create_table(
        "dependencies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src", sa.String(), nullable=False),
        sa.Column("dst", sa.String(), nullable=False),
        sa.Column("dep_type", sa.String(), server_default="depends_on"),
    )
    op.create_table(
        "doc_entities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_value", sa.String(), nullable=False),
    )
    op.create_index("ix_doc_entities_doc_id", "doc_entities", ["doc_id"])
    op.create_table(
        "ingested_docs",
        sa.Column("doc_id", sa.String(), primary_key=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("chunk_ids", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ingested_docs")
    op.drop_index("ix_doc_entities_doc_id", table_name="doc_entities")
    op.drop_table("doc_entities")
    op.drop_table("dependencies")
    op.drop_table("error_codes")
    op.drop_table("services")
    op.drop_index("ix_conversation_turns_tenant_id", table_name="conversation_turns")
    op.drop_index(
        "ix_conversation_turns_conversation_id", table_name="conversation_turns"
    )
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")
