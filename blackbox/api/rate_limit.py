"""Rate limiting that works across API replicas.

Redis is authoritative when available; a local limiter preserves behavior for dev and keeps
the API available if Redis is down (rate limiting fails open rather than taking down chat).
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class RateLimiter(Protocol):
    async def check(
        self, key: str, *, limit: int, window_s: float
    ) -> tuple[bool, int]: ...


class LocalRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(
        self, key: str, *, limit: int, window_s: float
    ) -> tuple[bool, int]:
        now = time.monotonic()
        async with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window_s:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(window_s - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            if len(self._hits) > 4096:
                self._hits = defaultdict(
                    deque, {k: v for k, v in self._hits.items() if v}
                )
            return True, 0


class RedisRateLimiter:
    """Fixed-window counter shared by all pods.

    `INCR` is atomic; the first increment sets the expiry. A Redis failure returns "allowed"
    so a cache outage cannot take chat down.
    """

    def __init__(self, client, *, prefix: str = "bb:rl:") -> None:
        self.client = client
        self.prefix = prefix

    async def check(
        self, key: str, *, limit: int, window_s: float
    ) -> tuple[bool, int]:
        bucket = int(time.time() // max(1.0, window_s))
        full_key = f"{self.prefix}{key}:{bucket}"
        try:
            count = await self.client.incr(full_key)
            if count == 1:
                await self.client.expire(full_key, int(window_s) + 1)
            if count > limit:
                ttl = await self.client.ttl(full_key)
                return False, max(1, int(ttl) if ttl and ttl > 0 else 1)
            return True, 0
        except Exception as exc:  # noqa: BLE001 - rate limiting fails open
            logger.warning("rate_limit_backend_failed", error=str(exc))
            return True, 0


async def build_rate_limiter(settings, config, fallback: RateLimiter) -> RateLimiter:
    if not config.redis.enabled or not settings.redis_url:
        return fallback
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url,
            socket_timeout=max(0.01, config.redis.timeout_ms / 1000),
            socket_connect_timeout=max(0.01, config.redis.timeout_ms / 1000),
            decode_responses=True,
        )
        await asyncio.wait_for(client.ping(), timeout=config.redis.timeout_ms / 1000)
        logger.info("rate_limiter_backend", backend="redis")
        return RedisRateLimiter(client)
    except Exception as exc:  # noqa: BLE001 - local fallback
        logger.warning("rate_limiter_redis_unavailable", error=str(exc), fallback="local")
        return fallback
