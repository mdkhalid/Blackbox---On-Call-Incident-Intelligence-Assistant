"""Timeout guard for retrieval.

A hanging vector store must not hang the request. Async callers get a real deadline;
sync callers (eval scripts, notebooks) get a cooperative budget check between stages,
since there is no safe way to interrupt a blocking C call from the same thread.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from blackbox.logging_setup import get_logger

T = TypeVar("T")

logger = get_logger(__name__)


class RetrievalTimeoutError(Exception):
    """Raised when retrieval exceeds its configured budget."""


async def with_timeout(
    awaitable: Awaitable[T], timeout_ms: int, *, operation: str = "retrieval"
) -> T:
    seconds = max(timeout_ms, 1) / 1000.0
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError as e:
        logger.warning(
            "retrieval_timeout", operation=operation, timeout_ms=timeout_ms
        )
        raise RetrievalTimeoutError(
            f"{operation} exceeded {timeout_ms}ms"
        ) from e


class Deadline:
    """Cooperative deadline for synchronous retrieval stages."""

    def __init__(self, timeout_ms: int, *, operation: str = "retrieval"):
        self.timeout_ms = timeout_ms
        self.operation = operation
        self._start = time.monotonic()

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000.0

    @property
    def remaining_ms(self) -> float:
        return self.timeout_ms - self.elapsed_ms

    def check(self, stage: str) -> None:
        if self.remaining_ms <= 0:
            logger.warning(
                "retrieval_timeout",
                operation=self.operation,
                stage=stage,
                elapsed_ms=round(self.elapsed_ms, 1),
                timeout_ms=self.timeout_ms,
            )
            raise RetrievalTimeoutError(
                f"{self.operation} exceeded {self.timeout_ms}ms at stage '{stage}'"
            )

    def run(self, stage: str, fn: Callable[[], T]) -> T:
        self.check(stage)
        return fn()
