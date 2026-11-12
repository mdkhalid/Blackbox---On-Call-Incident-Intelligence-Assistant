from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_alembic_upgrade_and_downgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")

    from blackbox.config.settings import get_settings

    get_settings.cache_clear()

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert {
        "conversations",
        "conversation_turns",
        "services",
        "error_codes",
        "dependencies",
        "doc_entities",
        "ingested_docs",
        "alembic_version",
    } <= tables
    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversations")
    }
    turn_columns = {
        column["name"] for column in inspector.get_columns("conversation_turns")
    }
    assert {"tenant_id", "memory_json", "last_budget_json"} <= conversation_columns
    assert {"tenant_id", "chunks_json", "trace_json", "partition_month"} <= turn_columns

    command.downgrade(config, "base")
    remaining = set(inspect(create_engine(url)).get_table_names())
    assert "conversations" not in remaining
    assert "conversation_turns" not in remaining
