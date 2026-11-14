"""Global per-tenant LLM/token quota tests."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from blackbox.api.quotas import LocalTenantQuotaLimiter, build_tenant_quota_limiter
from blackbox.config.settings import Settings


def test_local_quota_allows_then_rejects_tokens():
    limiter = LocalTenantQuotaLimiter(token_limit=10, call_limit=100)

    async def scenario():
        assert await limiter.check("t1") == (True, "", 0)
        await limiter.record("t1", tokens=10, llm_calls=1)
        allowed, reason, retry_after = await limiter.check("t1")
        assert allowed is False
        assert reason == "tokens"
        assert retry_after >= 1

    asyncio.run(scenario())


def test_local_quota_rejects_llm_calls():
    limiter = LocalTenantQuotaLimiter(token_limit=100, call_limit=2)

    async def scenario():
        await limiter.record("t1", tokens=1, llm_calls=2)
        allowed, reason, _ = await limiter.check("t1")
        assert allowed is False
        assert reason == "llm_calls"

    asyncio.run(scenario())


def test_local_quota_is_per_tenant():
    limiter = LocalTenantQuotaLimiter(token_limit=5, call_limit=5)

    async def scenario():
        await limiter.record("t1", tokens=5, llm_calls=1)
        assert (await limiter.check("t1"))[0] is False
        assert (await limiter.check("t2"))[0] is True

    asyncio.run(scenario())


def test_build_returns_fallback_when_disabled():
    settings = Settings(tenant_quota_enabled=False, tenant_quota_tokens_per_day=5)

    async def scenario():
        from blackbox.api.quotas import LocalTenantQuotaLimiter

        fallback = LocalTenantQuotaLimiter(token_limit=5, call_limit=5)
        result = await build_tenant_quota_limiter(settings, settings, fallback)
        assert result is fallback

    asyncio.run(scenario())


def test_build_returns_fallback_without_redis():
    settings = Settings(
        tenant_quota_enabled=True,
        tenant_quota_tokens_per_day=5,
        redis_url="",
    )

    async def scenario():
        from blackbox.api.quotas import LocalTenantQuotaLimiter

        fallback = LocalTenantQuotaLimiter(token_limit=5, call_limit=5)
        result = await build_tenant_quota_limiter(settings, settings, fallback)
        assert result is fallback

    asyncio.run(scenario())


def test_cost_budget_blocks_and_records():
    import asyncio

    from blackbox.api.quotas import LocalTenantQuotaLimiter

    async def scenario():
        limiter = LocalTenantQuotaLimiter(
            token_limit=1_000_000, call_limit=1000, cost_limit_usd=1.0
        )
        assert (await limiter.check("t"))[0] is True
        await limiter.record("t", tokens=10, llm_calls=1, cost_usd=1.5)
        allowed, reason, _ = await limiter.check("t")
        assert allowed is False
        assert reason == "budget_usd"

    asyncio.run(scenario())


def test_zero_cost_budget_is_unlimited():
    import asyncio

    from blackbox.api.quotas import LocalTenantQuotaLimiter

    async def scenario():
        limiter = LocalTenantQuotaLimiter(
            token_limit=1_000_000, call_limit=1000, cost_limit_usd=0.0
        )
        await limiter.record("t", tokens=1, llm_calls=1, cost_usd=10_000.0)
        assert (await limiter.check("t"))[0] is True

    asyncio.run(scenario())


def test_quota_settings_are_validated():
    with pytest.raises(ValidationError):
        Settings(tenant_quota_tokens_per_day=0)
    with pytest.raises(ValidationError):
        Settings(tenant_quota_llm_calls_per_day=0)