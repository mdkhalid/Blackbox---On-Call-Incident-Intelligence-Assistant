"""Global, cross-pod per-tenant LLM and token quotas.

The per-pod admission control in `AppServices.try_acquire_tenant` only limits concurrent
turns on one replica. These quotas are the shared, durable version: they count tokens
and LLM calls against a Redis key with a daily expiry so every replica sees the same
budget. Redis is authoritative when available; a local counter keeps the feature working
in dev and fails open if Redis is down, the same policy as rate limiting.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class TenantQuotaExceeded(Exception):
    """Raised when a tenant has exhausted a daily quota."""


class TenantQuotaLimiter(Protocol):
    async def check(self, tenant_id: str) -> tuple[bool, str, int]: ...

    async def record(
        self, tenant_id: str, *, tokens: int, llm_calls: int, cost_usd: float = 0.0
    ) -> None: ...


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if tomorrow <= now:
        tomorrow = tomorrow.replace(day=tomorrow.day + 1)
    # datetime has no +days helper that rolls months; use timestamp math instead.
    midnight_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) + 86400
    return max(1, midnight_ts - int(now.timestamp()))


def _day_key() -> str:
    return datetime.now(UTC).date().isoformat()


class LocalTenantQuotaLimiter:
    def __init__(
        self, *, token_limit: int, call_limit: int, cost_limit_usd: float = 0.0
    ) -> None:
        self.token_limit = max(1, token_limit)
        self.call_limit = max(1, call_limit)
        # 0 disables the dollar budget.
        self.cost_limit_usd = max(0.0, float(cost_limit_usd))
        self._usage: dict[str, dict[str, float]] = defaultdict(
            lambda: {"tokens": 0, "calls": 0, "cost": 0.0}
        )
        self._day: str = _day_key()
        self._lock = asyncio.Lock()

    def _rollover(self) -> None:
        day = _day_key()
        if day != self._day:
            self._usage.clear()
            self._day = day

    async def check(self, tenant_id: str) -> tuple[bool, str, int]:
        async with self._lock:
            self._rollover()
            usage = self._usage[tenant_id]
            if usage["tokens"] >= self.token_limit:
                return False, "tokens", _seconds_until_utc_midnight()
            if usage["calls"] >= self.call_limit:
                return False, "llm_calls", _seconds_until_utc_midnight()
            if self.cost_limit_usd > 0 and usage["cost"] >= self.cost_limit_usd:
                return False, "budget_usd", _seconds_until_utc_midnight()
            return True, "", 0

    async def record(
        self, tenant_id: str, *, tokens: int, llm_calls: int, cost_usd: float = 0.0
    ) -> None:
        async with self._lock:
            self._rollover()
            usage = self._usage[tenant_id]
            usage["tokens"] += max(0, tokens)
            usage["calls"] += max(0, llm_calls)
            usage["cost"] += max(0.0, float(cost_usd))


class RedisTenantQuotaLimiter:
    def __init__(
        self,
        client,
        *,
        token_limit: int,
        call_limit: int,
        cost_limit_usd: float = 0.0,
        prefix: str = "bb:quota:",
    ) -> None:
        self.client = client
        self.token_limit = max(1, token_limit)
        self.call_limit = max(1, call_limit)
        self.cost_limit_usd = max(0.0, float(cost_limit_usd))
        self.prefix = prefix

    def _keys(self, tenant_id: str) -> tuple[str, str, str]:
        day = _day_key()
        return (
            f"{self.prefix}tokens:{tenant_id}:{day}",
            f"{self.prefix}calls:{tenant_id}:{day}",
            f"{self.prefix}cost:{tenant_id}:{day}",
        )

    async def check(self, tenant_id: str) -> tuple[bool, str, int]:
        token_key, call_key, cost_key = self._keys(tenant_id)
        try:
            token_raw, call_raw, cost_raw = await self.client.mget(
                token_key, call_key, cost_key
            )
            tokens = int(token_raw) if token_raw is not None else 0
            calls = int(call_raw) if call_raw is not None else 0
            cost = float(cost_raw) if cost_raw is not None else 0.0
            if tokens >= self.token_limit:
                return False, "tokens", _seconds_until_utc_midnight()
            if calls >= self.call_limit:
                return False, "llm_calls", _seconds_until_utc_midnight()
            if self.cost_limit_usd > 0 and cost >= self.cost_limit_usd:
                return False, "budget_usd", _seconds_until_utc_midnight()
            return True, "", 0
        except Exception as exc:  # noqa: BLE001 - quotas fail open
            logger.warning("tenant_quota_backend_failed", error=str(exc))
            return True, "", 0

    async def record(
        self, tenant_id: str, *, tokens: int, llm_calls: int, cost_usd: float = 0.0
    ) -> None:
        token_key, call_key, cost_key = self._keys(tenant_id)
        ttl = _seconds_until_utc_midnight()
        try:
            pipe = self.client.pipeline()
            if tokens > 0:
                pipe.incrby(token_key, tokens)
            if llm_calls > 0:
                pipe.incrby(call_key, llm_calls)
            if cost_usd > 0:
                pipe.incrbyfloat(cost_key, float(cost_usd))
            await pipe.execute()
            # Set the expiry on first write. EXPIRE ... NX avoids clobbering a
            # shorter TTL on a later increment.
            if tokens > 0:
                await self.client.expire(token_key, ttl, nx=True)
            if llm_calls > 0:
                await self.client.expire(call_key, ttl, nx=True)
            if cost_usd > 0:
                await self.client.expire(cost_key, ttl, nx=True)
        except Exception as exc:  # noqa: BLE001 - quotas fail open
            logger.warning("tenant_quota_record_failed", error=str(exc))


async def build_tenant_quota_limiter(settings, config, fallback: TenantQuotaLimiter) -> TenantQuotaLimiter:
    if not settings.tenant_quota_enabled:
        return fallback
    if not settings.redis_url:
        logger.info("tenant_quota_backend", backend="local")
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
        logger.info("tenant_quota_backend", backend="redis")
        return RedisTenantQuotaLimiter(
            client,
            token_limit=settings.tenant_quota_tokens_per_day,
            call_limit=settings.tenant_quota_llm_calls_per_day,
            cost_limit_usd=settings.tenant_budget_usd_per_day,
        )
    except Exception as exc:  # noqa: BLE001 - local fallback
        logger.warning("tenant_quota_redis_unavailable", error=str(exc), fallback="local")
        return fallback