"""Tenant home-region resolution and write pinning.

In a multi-region deployment a tenant's conversation belongs to exactly one region at
a time. The mapping lives in the database (`tenant_regions`), not in the token: after a
failover an operator moves the tenant, and every replica must see the move. The JWT's
`region` claim, when present, is advisory only.

Resolution is cached in-process for a short TTL because it is read on every request; a
move or failover invalidates the cache explicitly, and the TTL bounds how long a stale
entry can survive if an invalidation is missed.
"""
from __future__ import annotations

import threading
import time
from typing import Protocol


class WrongRegion(Exception):
    """A write (or home-region read) arrived at a region that does not own the tenant."""

    def __init__(self, *, tenant_id: str, home_region: str, region: str) -> None:
        super().__init__(
            f"tenant {tenant_id} is homed in {home_region}, not {region}"
        )
        self.tenant_id = tenant_id
        self.home_region = home_region
        self.region = region


class TenantRegionRepository(Protocol):
    def home_region(self, tenant_id: str) -> str | None: ...

    def set_home_region(
        self, tenant_id: str, region: str, *, status: str = "active"
    ) -> None: ...


class TenantRegionResolver:
    def __init__(
        self,
        repository: TenantRegionRepository,
        *,
        current_region: str,
        default_region: str,
        cache_ttl_s: int = 60,
    ) -> None:
        self.repository = repository
        self.current_region = current_region
        self.default_region = default_region
        self.cache_ttl_s = max(0, cache_ttl_s)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def home_region(self, tenant_id: str) -> str:
        """The tenant's home region, from the cache or the database."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(tenant_id)
            if cached is not None and cached[1] > now:
                return cached[0]

        region = self.repository.home_region(tenant_id) or self.default_region
        with self._lock:
            self._cache[tenant_id] = (region, now + self.cache_ttl_s)
        return region

    def assert_write_region(self, tenant_id: str) -> str:
        """Raise `WrongRegion` when this instance does not own the tenant."""
        home = self.home_region(tenant_id)
        if home != self.current_region:
            raise WrongRegion(
                tenant_id=tenant_id, home_region=home, region=self.current_region
            )
        return home

    def set_home_region(
        self, tenant_id: str, region: str, *, status: str = "active"
    ) -> None:
        self.repository.set_home_region(tenant_id, region, status=status)
        self.invalidate(tenant_id)

    def invalidate(self, tenant_id: str | None = None) -> None:
        with self._lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant_id, None)
