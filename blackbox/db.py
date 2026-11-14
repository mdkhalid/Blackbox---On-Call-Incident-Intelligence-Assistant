"""Shared SQLAlchemy engine construction.

Both the conversation store and the entity store need the same engine rules, and both
need to get one detail right: what an in-memory SQLite database actually is.

Multi-region adds two concerns here: pool behaviour that survives a failover endpoint
flip, and the Postgres session GUC the row-level-security policies read.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Engine, MetaData, create_engine, event, inspect
from sqlalchemy.pool import StaticPool

# The tenant bound to the current request/job. The RLS hook reads it when a
# transaction begins; the middleware/worker sets it with `set_current_tenant`.
_current_tenant: ContextVar[str | None] = ContextVar("blackbox_tenant_id", default=None)


def is_memory_sqlite(db_url: str) -> bool:
    """Whether the URL names a SQLite database that lives in memory."""
    if not db_url.startswith("sqlite"):
        return False
    return ":memory:" in db_url or db_url.rstrip("/") in ("sqlite:", "sqlite")


def build_engine(
    db_url: str,
    *,
    pool_pre_ping: bool = False,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_recycle: int | None = None,
    **kwargs,
) -> Engine:
    """Create an engine whose in-memory SQLite database is shared across threads.

    A SQLite `:memory:` database belongs to a *connection*, and SQLAlchemy's default
    pool for SQLite hands every thread its own connection - which means a
    thread-local, empty database. The API runs its database calls in a thread pool,
    so an in-memory configuration would look empty from most requests: a conversation
    written on one thread would be missing on the next.

    A static pool keeps one connection, and therefore one database, for the whole
    process. File-backed SQLite and Postgres are unaffected.

    Postgres additionally gets the pooling policy passed in: `pool_pre_ping` makes a
    failover endpoint flip heal instead of surfacing a stale-connection error, and the
    size bounds keep one replica from exhausting the connection budget.
    """
    if is_memory_sqlite(db_url):
        return create_engine(
            db_url,
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            **kwargs,
        )
    if db_url.startswith("sqlite"):
        return create_engine(
            db_url,
            future=True,
            connect_args={"check_same_thread": False},
            **kwargs,
        )

    pool_kwargs: dict = {}
    if pool_size is not None:
        pool_kwargs["pool_size"] = pool_size
    if max_overflow is not None:
        pool_kwargs["max_overflow"] = max_overflow
    if pool_recycle:
        pool_kwargs["pool_recycle"] = pool_recycle
    if pool_pre_ping:
        pool_kwargs["pool_pre_ping"] = True
    return create_engine(db_url, future=True, **pool_kwargs, **kwargs)


@contextmanager
def set_current_tenant(tenant_id: str | None) -> Iterator[None]:
    """Bind the tenant for the duration of a request or job.

    The RLS hook reads this when a transaction begins. Every query executed inside the
    block is scoped by the database as well as by the application.
    """
    token = _current_tenant.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)


def current_tenant() -> str | None:
    return _current_tenant.get()


def install_rls_hook(engine: Engine, *, enabled: bool) -> None:
    """Feed `app.tenant_id` to the Postgres RLS policies on transaction begin.

    The policies created by migration 0002 compare `tenant_id` to
    `current_setting('app.tenant_id', true)`. Nothing in the application used to set
    that GUC, so enabling RLS would have made every row invisible (or, worse, every
    write fail the `WITH CHECK`). The hook sets it per transaction - `set_config(...,
    is_local=true)` is the parameterised form of `SET LOCAL`, and it is reset when the
    transaction ends, so a pooled connection cannot leak one tenant's identity into the
    next request.

    A no-op on any dialect without RLS, and when the feature is disabled.
    """
    if not enabled or engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine, "after_begin")
    def _apply_tenant(session, transaction, connection):
        tenant = _current_tenant.get()
        if tenant:
            connection.exec_driver_sql(
                "SELECT set_config('app.tenant_id', %s, true)", (tenant,)
            )


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
