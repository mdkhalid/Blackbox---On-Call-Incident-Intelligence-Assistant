"""Global per-tenant turn admission.

`AppServices` used to admit turns with a per-process counter, which meant N replicas
allowed N times the configured concurrency. This is the shared version: a Redis counter
per tenant, incremented on admission and decremented on release, with a lease TTL so a
pod that dies mid-turn cannot hold a slot forever. When Redis is unreachable the local
counter takes over — the same fail-open policy as rate limiting and quotas.

The counter is a *slot* lease, not a queue: a tenant over the limit is rejected with 429
rather than enqueued, which matches the per-pod behaviour it replaces.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class TenantConcurrencyLimiter(Protocol):
    async def try_acquire(self, tenant_id: str, *, limit: int, lease_s: int) -> bool: ...

    async def release(self, tenant_id: str) -> None: ...


class LocalTenantConcurrencyLimiter:
    """Per-process fallback; identical to the old admission control."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def try_acquire(self, tenant_id: str, *, limit: int, lease_s: int) -> bool:
        async with self._lock:
            if self._counts[tenant_id] >= limit:
                return False
            self._counts[tenant_id] += 1
            return True

    async def release(self, tenant_id: str) -> None:
        async with self._lock:
            count = self._counts.get(tenant_id, 0)
            if count <= 1:
                self._counts.pop(tenant_id, None)
            else:
                self._counts[tenant_id] = count - 1


class RedisTenantConcurrencyLimiter:
    """Shared counter with a lease TTL."""

    def __init__(self, client, *, prefix: str = "bb:concurrent:") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, tenant_id: str) -> str:
        return f"{self.prefix}{tenant_id}"

    async def try_acquire(self, tenant_id: str, *, limit: int, lease_s: int) -> bool:
        key = self._key(tenant_id)
        try:
            count = int(await self.client.incr(key))
            if count > limit:
                await self.client.decr(key)
                return False
            # The TTL is refreshed on every admission, so a crashed pod's slots expire
            # instead of pinning the tenant out of the service.
            await self.client.expire(key, max(1, lease_s))
            return True
        except Exception as exc:  # noqa: BLE001 - admission must not fail closed on Redis
            logger.warning("tenant_concurrency_redis_failed", error=str(exc))
            return True

    async def release(self, tenant_id: str) -> None:
        key = self._key(tenant_id)
        try:
            remaining = int(await self.client.decr(key))
            if remaining <= 0:
                await self.client.delete(key)
        except Exception as exc:  # noqa: BLE001 - the lease TTL is the backstop
            logger.warning("tenant_concurrency_release_failed", error=str(exc))


async def build_tenant_concurrency_limiter(
    settings, config, fallback: TenantConcurrencyLimiter
) -> TenantConcurrencyLimiter:
    """Redis when configured and reachable, the local fallback otherwise."""
    if not getattr(settings, "tenant_concurrency_global", True):
        return fallback
    if not settings.redis_url:
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
        logger.info("tenant_concurrency_backend", backend="redis")
        return RedisTenantConcurrencyLimiter(client)
    except Exception as exc:  # noqa: BLE001 - local fallback
        logger.warning(
            "tenant_concurrency_redis_unavailable", error=str(exc), fallback="local"
        )
        return fallback
