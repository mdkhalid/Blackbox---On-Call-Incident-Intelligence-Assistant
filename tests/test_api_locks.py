import pytest

from blackbox.api.locks import (
    ConversationBusy,
    LocalTurnLock,
    PostgresTurnLock,
    acquire_postgres_lock,
    advisory_key,
)
from blackbox.api.services import AppServices, build_lock_engine
from blackbox.config.settings import AppConfig


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeConnection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.closed = False
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        return FakeResult(self.acquired)

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, acquired=True):
        self.connection = FakeConnection(acquired)

    def connect(self):
        return self.connection


def test_advisory_key_is_a_stable_signed_64_bit_value():
    for conversation_id in ("conv-1", "a" * 64, "ümlaut"):
        key = advisory_key(conversation_id)
        assert -(2**63) <= key < 2**63
        assert key == advisory_key(conversation_id)


async def test_local_turn_lock_is_exclusive_and_releases(settings):
    services = AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=None,
        storage_backend="sqlite",
    )
    lock = await services.acquire_turn_lock("conv-1")
    assert isinstance(lock, LocalTurnLock)
    with pytest.raises(ConversationBusy):
        await services.acquire_turn_lock("conv-1")
    await lock.release()

    again = await services.acquire_turn_lock("conv-1")
    await again.release()


async def test_postgres_lock_uses_advisory_lock_and_closes_connection():
    engine = FakeEngine(acquired=True)
    lock = await acquire_postgres_lock(engine, "conv-1")
    assert isinstance(lock, PostgresTurnLock)
    await lock.release()
    assert engine.connection.closed is True
    assert any("pg_try_advisory_lock" in statement for statement, _ in engine.connection.executed)
    assert any("pg_advisory_unlock" in statement for statement, _ in engine.connection.executed)


async def test_postgres_lock_returns_none_when_busy():
    engine = FakeEngine(acquired=False)
    lock = await acquire_postgres_lock(engine, "conv-1")
    assert lock is None
    assert engine.connection.closed is True


def test_lock_engine_uses_null_pool():
    from sqlalchemy.pool import NullPool

    engine = build_lock_engine("sqlite:///:memory:")
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        engine.dispose()
