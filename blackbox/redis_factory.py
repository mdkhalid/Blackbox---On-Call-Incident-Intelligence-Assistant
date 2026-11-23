"""The one place that knows how to build a Redis client.

Cache, job queue, rate limiting, quotas and admission each used to call
`redis.from_url` with their own subset of options. The options drifted: one set a
socket timeout, another did not, and only the cache decoded responses. A single
factory keeps them consistent and makes the timeout policy reviewable in one place.

Redis is optional everywhere it is used; callers still decide whether a missing
client means "fall back" or "disable the feature".
"""
from __future__ import annotations

from typing import Any


def build_sync_client(
    url: str,
    *,
    timeout_s: float | None = 5.0,
    decode_responses: bool = True,
) -> Any:
    """Build a synchronous redis-py client.

    `timeout_s=None` leaves the socket without a timeout, which the job queue needs:
    its `BLPOP` blocks for a bounded period, and a shorter socket timeout would abort
    a legitimate wait.
    """
    import redis

    kwargs: dict[str, Any] = {"decode_responses": decode_responses}
    if timeout_s is not None:
        bounded = max(0.01, float(timeout_s))
        kwargs["socket_timeout"] = bounded
        kwargs["socket_connect_timeout"] = bounded
    return redis.Redis.from_url(url, **kwargs)


def build_async_client(
    url: str,
    *,
    timeout_s: float = 5.0,
    decode_responses: bool = True,
) -> Any:
    """Build a redis.asyncio client with the same option policy as the sync one."""
    import redis.asyncio as aioredis

    bounded = max(0.01, float(timeout_s))
    return aioredis.from_url(
        url,
        socket_timeout=bounded,
        socket_connect_timeout=bounded,
        decode_responses=decode_responses,
    )
