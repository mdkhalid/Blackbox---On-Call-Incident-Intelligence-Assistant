"""SSE cancellation: a client that goes away must stop the graph, not just the writer."""
from __future__ import annotations

import asyncio

import pytest

from blackbox.api.sse import ClientDisconnected, stream_with_disconnect


class FakeRequest:
    """Minimal stand-in for a Starlette request's disconnect probe."""

    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self.calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return (
            self._disconnect_after is not None
            and self.calls >= self._disconnect_after
        )


def test_events_pass_through_while_the_client_stays_connected():
    async def source():
        for i in range(3):
            yield i

    async def run():
        return [
            item
            async for item in stream_with_disconnect(
                FakeRequest(), source(), poll_s=0.01
            )
        ]

    assert asyncio.run(run()) == [0, 1, 2]


def test_a_disconnect_raises_and_closes_the_source():
    closed = asyncio.Event()

    async def never_ending():
        try:
            yield "first"
            await asyncio.sleep(10)
            yield "unreachable"
        finally:
            closed.set()

    async def run():
        stream = stream_with_disconnect(
            FakeRequest(disconnect_after=1), never_ending(), poll_s=0.01
        )
        first = await anext(stream)
        with pytest.raises(ClientDisconnected):
            async for _ in stream:
                pass
        return first

    assert asyncio.run(run()) == "first"
    assert closed.is_set()


def test_a_source_failure_is_not_reported_as_a_clean_end():
    class Boom(RuntimeError):
        pass

    async def broken():
        yield "one"
        raise Boom("graph failed")

    async def run():
        seen = []
        with pytest.raises(Boom):
            async for item in stream_with_disconnect(
                FakeRequest(), broken(), poll_s=0.01
            ):
                seen.append(item)
        return seen

    assert asyncio.run(run()) == ["one"]


def test_an_empty_stream_finishes_normally():
    async def empty():
        return
        yield  # pragma: no cover - makes this an async generator

    async def run():
        return [item async for item in stream_with_disconnect(FakeRequest(), empty())]

    assert asyncio.run(run()) == []


def test_turn_cleanup_completes_even_when_the_task_is_cancelled():
    """A cancelled SSE task must still release the lock, slot and idempotency key.

    The route awaits `asyncio.shield(cleanup)`; without the shield a second
    CancelledError interrupts the release and leaks the lease until its TTL.
    """
    from blackbox.api.routes.chat import _release_turn

    released: list[str] = []

    class Lock:
        async def release(self):
            # Slow enough that the cancellation arrives while cleanup is in flight.
            await asyncio.sleep(0.05)
            released.append("lock")

    class Services:
        def release_tenant(self, tenant_id):
            released.append("tenant")

        async def release_tenant_slot(self, tenant_id):
            released.append("slot")

    async def release_idempotency():
        released.append("idempotency")

    async def run():
        cleanup = _release_turn(
            turn_lock=Lock(),
            services=Services(),
            tenant_id="t1",
            release_idempotency=release_idempotency,
            conversation_id="c1",
        )
        async def shielded():
            await asyncio.shield(cleanup)

        task = asyncio.create_task(shielded())
        await asyncio.sleep(0)  # let the shielded cleanup start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The shielded cleanup keeps running on its own task; let it finish.
        for _ in range(100):
            if set(released) == {"lock", "tenant", "slot", "idempotency"}:
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert set(released) == {"lock", "tenant", "slot", "idempotency"}


def test_a_non_positive_poll_interval_is_rejected():
    async def source():
        yield 1

    with pytest.raises(ValueError):

        async def run():
            async for _ in stream_with_disconnect(FakeRequest(), source(), poll_s=0):
                pass

        asyncio.run(run())
