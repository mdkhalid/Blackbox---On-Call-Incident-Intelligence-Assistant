"""Shared SQLAlchemy engine construction.

Both the conversation store and the entity store need the same engine rules, and both
need to get one detail right: what an in-memory SQLite database actually is.
"""
from __future__ import annotations

from sqlalchemy import Engine, MetaData, create_engine, inspect
from sqlalchemy.pool import StaticPool


def is_memory_sqlite(db_url: str) -> bool:
    """Whether the URL names a SQLite database that lives in memory."""
    if not db_url.startswith("sqlite"):
        return False
    return ":memory:" in db_url or db_url.rstrip("/") in ("sqlite:", "sqlite")


def build_engine(db_url: str, **kwargs) -> Engine:
    """Create an engine whose in-memory SQLite database is shared across threads.

    A SQLite `:memory:` database belongs to a *connection*, and SQLAlchemy's default
    pool for SQLite hands every thread its own connection - which means a
    thread-local, empty database. The API runs its database calls in a thread pool,
    so an in-memory configuration would look empty from most requests: a conversation
    written on one thread would be missing on the next.

    A static pool keeps one connection, and therefore one database, for the whole
    process. File-backed SQLite and Postgres are unaffected.
    """
    if is_memory_sqlite(db_url):
        return create_engine(
            db_url,
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            **kwargs,
        )
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, future=True, connect_args=connect_args, **kwargs)


def schema_is_current(engine: Engine, metadata: MetaData) -> bool:
    """Whether the database's tables match the models.

    `create_all` creates missing tables but never alters an existing one, so a
    database written by an older version of the models keeps its old columns and
    every query against a new column fails with "no such column".
    """
    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            return False
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        expected = {column.name for column in table.columns}
        if not expected <= actual:
            return False
    return True


def recreate(engine: Engine, metadata: MetaData) -> None:
    """Drop and recreate the tables. Destroys everything stored in them."""
    metadata.drop_all(engine)
    metadata.create_all(engine)
