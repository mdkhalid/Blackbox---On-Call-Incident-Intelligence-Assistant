"""Cached embedding provider.

Embeddings are the most repeated expensive operation (retrieval plus query rewrites), so
keying them by model + text is safe and pays off immediately. A cache failure is a miss.
"""
from __future__ import annotations

import hashlib

from blackbox.cache.backend import CacheBackend


class CachedEmbeddingProvider:
    def __init__(
        self,
        provider,
        cache: CacheBackend,
        *,
        ttl_s: float = 604800,
        prefix: str = "emb:",
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.ttl_s = ttl_s
        self.prefix = prefix

    def _key(self, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self.prefix}{self.provider.name}:{digest}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str, str]] = []
        for index, text in enumerate(texts):
            key = self._key(text)
            cached = self.cache.get(key)
            if isinstance(cached, list) and len(cached) == self.dim:
                results[index] = cached
            else:
                missing.append((index, text, key))

        if missing:
            vectors = self.provider.embed([text for _, text, _ in missing])
            for (index, _, key), vector in zip(missing, vectors, strict=False):
                results[index] = vector
                self.cache.set(key, vector, self.ttl_s)

        return [vector or [] for vector in results]

    @property
    def dim(self) -> int:
        return self.provider.dim

    @property
    def name(self) -> str:
        return self.provider.name
