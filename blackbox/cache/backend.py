"""Cache backends with graceful degradation.

Redis is optional by design: every operation is best-effort, a failure is a cache miss, and
an in-process TTL cache is the fallback. Keys are namespaced so ingest can invalidate only
retrieval entries.
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class CacheBackend(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None: ...
    def set_if_absent(
        self, key: str, value: Any, ttl_s: float | None = None
    ) -> bool: ...
    def delete(self, key: str) -> None: ...
    def clear_prefix(self, prefix: str) -> None: ...
    def clear(self) -> None: ...


class TTLCache:
    """Thread-safe in-process LRU with per-entry expiry."""

    def __init__(self, default_ttl_s: float = 300, max_entries: int = 4096) -> None:
        self.default_ttl_s = float(default_ttl_s)
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        ttl = self.default_ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def set_if_absent(
        self, key: str, value: Any, ttl_s: float | None = None
    ) -> bool:
        ttl = self.default_ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and entry[0] > now:
                return False
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [key for key in self._data if key.startswith(prefix)]:
                self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RedisCache:
    """JSON cache over redis-py. Every failure degrades to a miss."""

    def __init__(self, client, *, prefix: str = "bb:", default_ttl_s: float = 300) -> None:
        self.client = client
        self.prefix = prefix
        self.default_ttl_s = float(default_ttl_s)

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str) -> Any | None:
        try:
            raw = self.client.get(self._full(key))
        except Exception as exc:  # noqa: BLE001 - cache failures are misses
            logger.warning("cache_get_failed", error=str(exc))
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        ttl = self.default_ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            return
        try:
            self.client.setex(
                self._full(key), max(1, int(ttl)), json.dumps(value, default=str)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_set_failed", error=str(exc))

    def set_if_absent(
        self, key: str, value: Any, ttl_s: float | None = None
    ) -> bool:
        ttl = self.default_ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            return False
        try:
            result = self.client.set(
                self._full(key),
                json.dumps(value, default=str),
                nx=True,
                ex=max(1, int(ttl)),
            )
        except Exception as exc:  # noqa: BLE001 - fail open for idempotency locks
            logger.warning("cache_setnx_failed", error=str(exc))
            return True
        return bool(result)

    def delete(self, key: str) -> None:
        try:
            self.client.delete(self._full(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_delete_failed", error=str(exc))

    def clear_prefix(self, prefix: str) -> None:
        try:
            keys = list(self.client.scan_iter(match=f"{self.prefix}{prefix}*"))
            if keys:
                self.client.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_clear_failed", error=str(exc))

    def clear(self) -> None:
        self.clear_prefix("")


def build_cache(settings, config) -> CacheBackend | None:
    """Redis when reachable, in-memory otherwise. `enabled: false` disables caching."""
    if not config.redis.enabled:
        return None

    default_ttl = max(1, config.redis.ttl_retrieval_s)
    if settings.redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                socket_timeout=max(0.01, config.redis.timeout_ms / 1000),
                socket_connect_timeout=max(0.01, config.redis.timeout_ms / 1000),
                decode_responses=True,
            )
            client.ping()
            logger.info("cache_backend", backend="redis")
            return RedisCache(client, default_ttl_s=default_ttl)
        except Exception as exc:  # noqa: BLE001 - fall back, never fail startup
            logger.warning("redis_unavailable", error=str(exc), fallback="memory")

    return TTLCache(default_ttl_s=default_ttl)
