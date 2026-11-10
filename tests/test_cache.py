import json

from blackbox.cache import CachedEmbeddingProvider, RedisCache, TTLCache
from blackbox.llm import FakeLLMProvider
from blackbox.llm.base import LLMResponse
from blackbox.llm.cached import CachedLLMProvider


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttls[key] = ttl

    def scan_iter(self, match):
        prefix = match.rstrip("*")
        return [key for key in list(self.data) if key.startswith(prefix)]

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


def test_ttl_cache_expires(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("blackbox.cache.backend.time.monotonic", lambda: clock["now"])
    cache = TTLCache(default_ttl_s=10)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    clock["now"] += 11
    assert cache.get("k") is None


def test_ttl_cache_clears_only_matching_prefix():
    cache = TTLCache(default_ttl_s=10)
    cache.set("ret:a", 1)
    cache.set("emb:b", 2)
    cache.clear_prefix("ret:")
    assert cache.get("ret:a") is None
    assert cache.get("emb:b") == 2


def test_redis_cache_round_trip_and_prefix_clear():
    client = FakeRedis()
    cache = RedisCache(client, prefix="bb:", default_ttl_s=60)
    cache.set("ret:a", {"x": 1})
    assert cache.get("ret:a") == {"x": 1}
    assert client.ttls["bb:ret:a"] == 60
    cache.clear_prefix("ret:")
    assert cache.get("ret:a") is None


def test_redis_cache_failures_are_misses():
    class Broken:
        def get(self, key):
            raise RuntimeError("down")

        def setex(self, *args):
            raise RuntimeError("down")

        def scan_iter(self, match):
            raise RuntimeError("down")

        def delete(self, *keys):
            raise RuntimeError("down")

    cache = RedisCache(Broken())
    assert cache.get("k") is None
    cache.set("k", 1)
    cache.clear_prefix("ret:")


class CountingEmbedder:
    name = "fake-embed"
    dim = 2

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


def test_embedding_cache_avoids_reembedding():
    provider = CountingEmbedder()
    cache = TTLCache(default_ttl_s=60)
    wrapped = CachedEmbeddingProvider(provider, cache)

    first = wrapped.embed(["checkout pool"])
    second = wrapped.embed(["checkout pool"])

    assert provider.calls == 1
    assert first == second
    assert wrapped.name == "fake-embed"


class CountingLLM:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete(self, *, system, prompt, model=None, temperature=0.0, max_tokens=512, json_mode=False):
        self.calls += 1
        return LLMResponse(
            text=json.dumps({"n": self.calls}),
            model=model or "fake",
            prompt_tokens=3,
            completion_tokens=2,
        )


def test_llm_cache_hits_and_marks_cached():
    provider = CountingLLM()
    cache = TTLCache(default_ttl_s=60)
    wrapped = CachedLLMProvider(provider, cache)

    first = wrapped.complete(system="s", prompt="p", model="m", max_tokens=10)
    second = wrapped.complete(system="s", prompt="p", model="m", max_tokens=10)

    assert provider.calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == first.text


def test_llm_cache_key_includes_generation_parameters():
    provider = CountingLLM()
    wrapped = CachedLLMProvider(provider, TTLCache(default_ttl_s=60))

    wrapped.complete(system="s", prompt="p", model="m", temperature=0.0)
    wrapped.complete(system="s", prompt="p", model="m", temperature=0.2)
    wrapped.complete(system="s", prompt="p", model="other", temperature=0.0)

    assert provider.calls == 3


def test_cached_llm_preserves_provider_name():
    wrapped = CachedLLMProvider(FakeLLMProvider(), TTLCache())
    assert wrapped.name == "fake"
