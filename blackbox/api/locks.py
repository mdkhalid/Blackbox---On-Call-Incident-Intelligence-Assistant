"""Per-conversation turn locks.

Single-process deployments use an asyncio lock. Postgres deployments use a session-level
advisory lock so two workers cannot run the same conversation concurrently. SQLite keeps
the local lock; advisory locks are Postgres-only by definition.
"""
from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool


class ConversationBusy(RuntimeError):
    pass


def advisory_key(conversation_id: str) -> int:
    """Stable signed 64-bit key for `pg_try_advisory_lock`."""
    raw = hashlib.sha256(conversation_id.encode("utf-8")).digest()[:8]
    value = int.from_bytes(raw, "big", signed=False)
    return value - 2**64 if value >= 2**63 else value


class LocalTurnLock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock

    async def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


class PostgresTurnLock:
    def __init__(self, connection, key: int) -> None:
        self._connection = connection
        self._key = key

    async def release(self) -> None:
        connection, key = self._connection, self._key

        def _release() -> None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
            finally:
                connection.close()

        await run_in_threadpool(_release)


async def acquire_postgres_lock(engine, conversation_id: str):
    key = advisory_key(conversation_id)

    def _acquire():
        connection = engine.connect()
        try:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar()
        except Exception:
            connection.close()
            raise
        if not acquired:
            connection.close()
            return None
        return connection

    connection = await run_in_threadpool(_acquire)
    return PostgresTurnLock(connection, key) if connection is not None else None
