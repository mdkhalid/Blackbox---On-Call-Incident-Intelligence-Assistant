"""Database bootstrap behaviour.

Two properties matter here, and both were bugs:

* A SQLite `:memory:` database belongs to a *connection*, and SQLAlchemy hands every
  thread its own connection. The API runs its database calls in a thread pool, so an
  in-memory configuration used to look empty from most requests.
* `create_all` creates missing tables but never alters an existing one, so a local
  database written by an older version of the models kept its old columns and every
  query against a new column failed with "no such column".
"""
import threading

from sqlalchemy import create_engine, text

from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig
from blackbox.db import build_engine, is_memory_sqlite


def test_an_in_memory_database_is_shared_across_threads():
    """A table written on one thread must be visible from another."""
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("create table t (x int)"))
        connection.execute(text("insert into t values (1)"))

    seen = []

    def read() -> None:
        with engine.connect() as connection:
            seen.append(connection.execute(text("select count(*) from t")).scalar())

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()

    assert seen == [1]


def test_the_in_memory_check_distinguishes_the_two_sqlite_forms():
    assert is_memory_sqlite("sqlite:///:memory:")
    assert is_memory_sqlite("sqlite://")
    assert not is_memory_sqlite("sqlite:///data/blackbox.db")
    assert not is_memory_sqlite("postgresql://host/db")


def _stale_database(path) -> None:
    """A database with the pre-`tenant_id` schema."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table conversations (id varchar primary key, "
                "memory_json json not null, last_budget_json json, "
                "created_at varchar not null, updated_at varchar not null)"
            )
        )
    engine.dispose()


def test_a_stale_local_database_is_detected(tmp_path):
    path = tmp_path / "stale.db"
    _stale_database(path)

    repo = SqlAlchemyConversationRepository(f"sqlite:///{path}")
    repo.init()
    assert not repo.schema_is_current()


def test_a_stale_local_database_is_recreated_outside_production(tmp_path, settings):
    """A developer whose local database predates a schema change gets a working app."""
    path = tmp_path / "stale.db"
    _stale_database(path)

    repo = SqlAlchemyConversationRepository(f"sqlite:///{path}")
    repo.init()
    services = AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=repo,
        storage_backend="sqlite",
    )
    services._recreate_if_stale(repo)

    assert repo.schema_is_current()
    # The recreated database is usable.
    record = repo.get_or_create("conv-1")
    assert record.id == "conv-1"


def test_a_stale_database_is_never_recreated_in_production(tmp_path, settings):
    """Production owns its schema with Alembic; the app must not drop its tables."""
    path = tmp_path / "stale.db"
    _stale_database(path)

    repo = SqlAlchemyConversationRepository(f"sqlite:///{path}")
    repo.init()
    prod_settings = settings.model_copy(update={"env": "prod"})
    services = AppServices(
        settings=prod_settings,
        config=AppConfig(),
        conversations=repo,
        storage_backend="sqlite",
    )
    services._recreate_if_stale(repo)

    assert not repo.schema_is_current()


def test_a_current_database_is_left_alone(tmp_path, settings):
    path = tmp_path / "current.db"
    repo = SqlAlchemyConversationRepository(f"sqlite:///{path}")
    repo.init()

    services = AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=repo,
        storage_backend="sqlite",
    )
    services._recreate_if_stale(repo)

    assert repo.schema_is_current()
