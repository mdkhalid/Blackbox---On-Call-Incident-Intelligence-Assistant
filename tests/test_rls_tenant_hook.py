"""The RLS tenant-GUC hook and tenant context binding."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from blackbox.db import (
    build_engine,
    current_tenant,
    install_rls_hook,
    set_current_tenant,
)


def test_current_tenant_defaults_to_none():
    assert current_tenant() is None


def test_set_current_tenant_is_scoped():
    with set_current_tenant("tenant-a"):
        assert current_tenant() == "tenant-a"
    assert current_tenant() is None


def test_hook_is_noop_on_sqlite():
    engine = build_engine("sqlite:///:memory:")
    install_rls_hook(engine, enabled=True)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1


def test_hook_is_noop_when_disabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    install_rls_hook(engine, enabled=False)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1


def test_pool_kwargs_ignored_for_sqlite():
    # The Postgres pool policy must not be forced onto a SQLite engine.
    engine = build_engine(
        "sqlite:///:memory:",
        pool_pre_ping=True,
        pool_size=4,
        max_overflow=2,
        pool_recycle=60,
    )
    with engine.begin() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="set POSTGRES_TEST_URL to run against a real Postgres with RLS enabled",
)
def test_rls_hides_other_tenants():
    engine = build_engine(os.environ["POSTGRES_TEST_URL"], pool_pre_ping=True)
    install_rls_hook(engine, enabled=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TEMP TABLE rls_probe (tenant_id text not null);"
            )
        )
        connection.execute(
            text("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
        )
        connection.execute(
            text(
                "CREATE POLICY p ON rls_probe "
                "USING (tenant_id = current_setting('app.tenant_id', true))"
            )
        )
        connection.execute(
            text("INSERT INTO rls_probe VALUES ('tenant-a'), ('tenant-b')")
        )
    with set_current_tenant("tenant-a"), engine.connect() as connection:
        rows = connection.execute(text("SELECT tenant_id FROM rls_probe")).all()
    assert rows == [("tenant-a",)]
