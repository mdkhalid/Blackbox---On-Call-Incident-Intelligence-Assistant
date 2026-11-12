"""SSE helpers shared by the chat route."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request

# How often an in-flight stream checks whether its consumer is still connected.
# The check is cheap (the ASGI server has already observed the disconnect); the
# poll only exists so a stream waiting on a long model call notices promptly.
_DISCONNECT_POLL_S = 0.25


class ClientDisconnected(Exception):
    """The SSE consumer went away before the stream finished.

    Raised from :func:`stream_with_disconnect` instead of silently returning, so the
    route can log the abandonment and skip work whose result nobody will receive.
    """

_CHUNK_FIELDS = (
    "chunk_id",
    "doc_id",
    "section",
    "doc_type",
    "dense_score",
    "dense_rank",
    "sparse_score",
    "sparse_rank",
    "fused_score",
    "fused_rank",
    "rerank_score",
    "rerank_rank",
    "score",
)


def sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def token_chunks(text: str, size: int = 24) -> list[str]:
    """Split the final, ground-checked answer into transport-sized pieces.

    These are not raw decode tokens: the ground check must run before any answer text is
    exposed, so token events intentionally start after `ground_check`/`honest_failure`.
    """
    return [text[i : i + size] for i in range(0, len(text), size)]


def chunk_trails(chunks: list[dict]) -> list[dict]:
    return [{key: chunk.get(key) for key in _CHUNK_FIELDS} for chunk in chunks]


def public_update(values: dict) -> dict:
    """Node update safe to put on the wire: no chunk text, no duplicated trace."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in ("trace", "answer"):
            continue
        if key == "chunks" and isinstance(value, list):
            out[key] = chunk_trails(value)
        else:
            out[key] = value
    return out


async def stream_with_disconnect(
    request: Request,
    events: AsyncIterator[Any],
    *,
    poll_s: float = _DISCONNECT_POLL_S,
) -> AsyncIterator[Any]:
    """Re-yield ``events``, aborting the source when the client disconnects.

    Iterating a graph is not enough on its own: a disconnect is only noticed when the
    next event is written, and a stream can wait a long time for a hosted model. The
    source is therefore pumped into a queue while a watcher polls
    ``request.is_disconnected()``. When the watcher fires, the producer is cancelled,
    which closes the graph's async generator at its current await point, and
    :class:`ClientDisconnected` is raised to the caller.

    The pump's exception, if it fails, is re-raised after the queue drains so a graph
    failure is not mistaken for a normal end of stream.
    """
    if poll_s <= 0:
        raise ValueError("poll_s must be > 0")

    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()
    failure: list[BaseException] = []

    async def _pump() -> None:
        try:
            async for item in events:
                await queue.put(item)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the consumer
            failure.append(exc)
        finally:
            queue.put_nowait(sentinel)

    async def _watch() -> None:
        while True:
            await asyncio.sleep(poll_s)
            try:
                if await request.is_disconnected():
                    return
            except Exception:  # noqa: BLE001 - a broken transport is a disconnect
                return

    producer = asyncio.create_task(_pump())
    watcher = asyncio.create_task(_watch())
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {getter, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await getter
                raise ClientDisconnected("the SSE client disconnected")
            item = getter.result()
            if item is sentinel:
                if failure:
                    raise failure[0]
                return
            yield item
    finally:
        for task in (watcher, producer):
            task.cancel()
        await asyncio.gather(watcher, producer, return_exceptions=True)
