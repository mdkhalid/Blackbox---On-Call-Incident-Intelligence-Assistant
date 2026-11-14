"""Global per-tenant turn admission."""
from __future__ import annotations

import asyncio

from blackbox.api.admission import (
    LocalTenantConcurrencyLimiter,
    RedisTenantConcurrencyLimiter,
    build_tenant_concurrency_limiter,
)
from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, Settings, get_settings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key):
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    async def ping(self):
        return True


def test_local_limiter_enforces_and_releases():
    async def scenario():
        limiter = LocalTenantConcurrencyLimiter()
        assert await limiter.try_acquire("t", limit=2, lease_s=10)
        assert await limiter.try_acquire("t", limit=2, lease_s=10)
        assert not await limiter.try_acquire("t", limit=2, lease_s=10)
        await limiter.release("t")
        assert await limiter.try_acquire("t", limit=2, lease_s=10)

    asyncio.run(scenario())


def test_local_limiter_is_per_tenant():
    async def scenario():
        limiter = LocalTenantConcurrencyLimiter()
        assert await limiter.try_acquire("a", limit=1, lease_s=10)
        assert await limiter.try_acquire("b", limit=1, lease_s=10)

    asyncio.run(scenario())


def test_redis_limiter_sets_a_lease_and_rolls_back_over_limit():
    async def scenario():
        client = FakeRedis()
        limiter = RedisTenantConcurrencyLimiter(client)
        assert await limiter.try_acquire("t", limit=1, lease_s=42)
        assert client.ttls["bb:concurrent:t"] == 42
        assert not await limiter.try_acquire("t", limit=1, lease_s=42)
        # The rejected increment was rolled back.
        assert client.values["bb:concurrent:t"] == 1
        await limiter.release("t")
        assert "bb:concurrent:t" not in client.values

    asyncio.run(scenario())


def test_redis_failure_fails_open():
    class Broken:
        async def incr(self, key):
            raise RuntimeError("down")

    async def scenario():
        limiter = RedisTenantConcurrencyLimiter(Broken())
        assert await limiter.try_acquire("t", limit=1, lease_s=10) is True

    asyncio.run(scenario())


def test_build_uses_local_without_redis():
    async def scenario():
        fallback = LocalTenantConcurrencyLimiter()
        settings = Settings(redis_url="")
        result = await build_tenant_concurrency_limiter(
            settings, AppConfig(), fallback
        )
        assert result is fallback

    asyncio.run(scenario())


def test_services_slot_uses_the_local_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    settings = get_settings()
    settings = settings.model_copy(update={"tenant_concurrency_global": False})
    services = AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=SqlAlchemyConversationRepository("sqlite:///:memory:"),
    )

    async def scenario():
        assert await services.acquire_tenant_slot("t")
        await services.release_tenant_slot("t")

    asyncio.run(scenario())
