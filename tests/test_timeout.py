import asyncio
import time

import pytest

from blackbox.retrieval.timeout import (
    Deadline,
    RetrievalTimeoutError,
    with_timeout,
)


async def _slow(seconds: float):
    await asyncio.sleep(seconds)
    return "done"


async def _fast():
    return "done"


async def test_with_timeout_returns_result_when_within_budget():
    assert await with_timeout(_fast(), timeout_ms=1000) == "done"


async def test_with_timeout_raises_when_budget_exceeded():
    with pytest.raises(RetrievalTimeoutError):
        await with_timeout(_slow(5), timeout_ms=10)


async def test_timeout_message_names_the_operation():
    with pytest.raises(RetrievalTimeoutError, match="vector_store"):
        await with_timeout(_slow(5), timeout_ms=10, operation="vector_store")


async def test_timeout_error_is_not_asyncio_timeout():
    # Callers should not need to know about asyncio internals.
    with pytest.raises(RetrievalTimeoutError) as exc:
        await with_timeout(_slow(5), timeout_ms=10)
    assert not isinstance(exc.value, asyncio.TimeoutError)


def test_deadline_reports_elapsed_and_remaining():
    d = Deadline(1000)
    assert d.elapsed_ms >= 0
    assert d.remaining_ms <= 1000
    d.check("anything")


def test_deadline_raises_once_expired():
    d = Deadline(1)
    time.sleep(0.02)
    assert d.remaining_ms < 0
    with pytest.raises(RetrievalTimeoutError):
        d.check("dense")


def test_deadline_run_executes_callable():
    d = Deadline(1000)
    assert d.run("stage", lambda: 42) == 42


def test_deadline_run_refuses_when_expired():
    d = Deadline(1)
    time.sleep(0.02)

    def should_not_run():
        raise AssertionError("callable must not run past the deadline")

    with pytest.raises(RetrievalTimeoutError):
        d.run("stage", should_not_run)


def test_deadline_message_includes_stage():
    d = Deadline(1, operation="hybrid_retrieve")
    time.sleep(0.02)
    with pytest.raises(RetrievalTimeoutError, match="dense"):
        d.check("dense")
