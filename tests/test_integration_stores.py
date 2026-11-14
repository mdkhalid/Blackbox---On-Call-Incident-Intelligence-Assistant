"""Optional integration tests against real Postgres/Redis.

Skipped unless the corresponding URL is provided, so the default suite stays offline.
CI can run them by provisioning the service and exporting the variable.

    POSTGRES_TEST_URL=postgresql://blackbox:blackbox@localhost:5432/blackbox \
    REDIS_TEST_URL=redis://localhost:6379/15 pytest tests/test_integration_stores.py
"""
from __future__ import annotations

import os
import uuid

import pytest

from blackbox.api.store import SqlAlchemyConversationRepository

POSTGRES_URL = os.environ.get("POSTGRES_TEST_URL", "")
REDIS_URL = os.environ.get("REDIS_TEST_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="set POSTGRES_TEST_URL to run")
def test_conversation_roundtrip_on_postgres():
    repo = SqlAlchemyConversationRepository(POSTGRES_URL)
    repo.init()
    tenant = f"it-{uuid.uuid4().hex[:8]}"
    conversation = f"it-{uuid.uuid4().hex[:8]}"
    try:
        repo.get_or_create(conversation, tenant_id=tenant)
        repo.append_turn(conversation, "q", "a", "answered", [], {}, tenant_id=tenant)

        record = repo.get(conversation, tenant_id=tenant)
        assert record is not None
        assert len(repo.get_turns(conversation, tenant_id=tenant)) == 1
        # Cross-tenant reads fail closed.
        assert repo.get(conversation, tenant_id=f"{tenant}-other") is None
    finally:
        repo.delete_tenant(tenant)


@pytest.mark.skipif(not REDIS_URL, reason="set REDIS_TEST_URL to run")
def test_shared_cache_roundtrip_on_redis():
    import redis

    from blackbox.cache.backend import RedisCache

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    cache = RedisCache(client, prefix=f"it-{uuid.uuid4().hex[:6]}:")
    key = "k"
    try:
        cache.set(key, {"value": 1}, ttl_s=30)
        assert cache.get(key) == {"value": 1}
        assert cache.set_if_absent(key, "other") is False
    finally:
        cache.clear()
        client.close()
