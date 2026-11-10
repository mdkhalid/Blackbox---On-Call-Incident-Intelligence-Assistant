from blackbox.api.rate_limit import LocalRateLimiter, RedisRateLimiter


async def test_local_limiter_enforces_limit_and_recovers():
    limiter = LocalRateLimiter()
    for _ in range(2):
        allowed, retry = await limiter.check("ip", limit=2, window_s=60)
        assert allowed is True
        assert retry == 0

    allowed, retry = await limiter.check("ip", limit=2, window_s=60)
    assert allowed is False
    assert retry >= 1


async def test_different_keys_have_independent_buckets():
    limiter = LocalRateLimiter()
    assert (await limiter.check("a", limit=1, window_s=60))[0] is True
    assert (await limiter.check("b", limit=1, window_s=60))[0] is True
    assert (await limiter.check("a", limit=1, window_s=60))[0] is False


class FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expiries = {}
        self.ttl_value = 42

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        self.expiries[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttl_value


async def test_redis_limiter_uses_shared_counter_and_ttl():
    client = FakeRedis()
    limiter = RedisRateLimiter(client, prefix="bb:rl:")

    first, _ = await limiter.check("1.2.3.4", limit=2, window_s=60)
    second, _ = await limiter.check("1.2.3.4", limit=2, window_s=60)
    third, retry = await limiter.check("1.2.3.4", limit=2, window_s=60)

    assert first and second
    assert third is False
    assert retry == 42
    key = next(iter(client.counts))
    assert key.startswith("bb:rl:1.2.3.4:")
    assert client.expiries[key] == 61


async def test_redis_limiter_fails_open_on_backend_error():
    class Broken:
        async def incr(self, key):
            raise RuntimeError("redis down")

    allowed, retry = await RedisRateLimiter(Broken()).check(
        "ip", limit=1, window_s=1
    )
    assert allowed is True
    assert retry == 0
