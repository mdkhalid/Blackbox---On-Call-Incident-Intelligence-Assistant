"""Shadow retrieval must never add its latency to the user's request."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from blackbox.retrieval.shadow import (
    ShadowingRetriever,
    pending_shadow_comparisons,
)


@dataclass
class Chunk:
    chunk_id: str


@dataclass
class Result:
    chunks: list[Chunk] = field(default_factory=list)


class Primary:
    def retrieve_detailed(self, query, **kwargs):
        return Result([Chunk("c1")])

    async def aretrieve_detailed(self, query, **kwargs):
        return Result([Chunk("c1")])


class SlowShadow:
    def __init__(self) -> None:
        self.completed = 0

    def retrieve_detailed(self, query, **kwargs):
        time.sleep(0.3)
        self.completed += 1
        return Result([Chunk("c1")])


def _wait_for_shadow(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while pending_shadow_comparisons() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_async_shadow_comparison_does_not_block_the_caller():
    shadow = SlowShadow()
    retriever = ShadowingRetriever(Primary(), shadow)

    async def run():
        started = time.perf_counter()
        result = await retriever.aretrieve_detailed("q")
        return result, time.perf_counter() - started

    result, elapsed = asyncio.run(run())
    assert result.chunks[0].chunk_id == "c1"
    assert elapsed < 0.2  # the 0.3s shadow is not on the caller's path
    _wait_for_shadow()
    assert shadow.completed == 1


def test_synchronous_shadow_comparison_does_not_block_the_caller():
    shadow = SlowShadow()
    retriever = ShadowingRetriever(Primary(), shadow)

    started = time.perf_counter()
    result = retriever.retrieve_detailed("q")
    elapsed = time.perf_counter() - started

    assert result.chunks[0].chunk_id == "c1"
    assert elapsed < 0.2
    _wait_for_shadow()
    assert shadow.completed == 1
