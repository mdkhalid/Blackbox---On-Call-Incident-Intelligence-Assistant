"""Shadow indexing and dual-read comparison for the retrieval migration.

During a backend migration the new store is run *alongside* the old one: every
query goes to the primary store, and the same query is replayed against the
shadow store in the background. The two result sets are compared and the
difference is logged so a regression in the new backend is visible before it
becomes the primary. The shadow result never reaches the caller.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class ShadowingRetriever:
    """Wraps a primary retriever and an optional shadow retriever.

    The primary's result is the one returned to the caller. The shadow is
    invoked on a worker thread and its result is compared (by chunk id set) to
    the primary's; mismatches are logged as ``retrieval_shadow_diff``.
    """

    def __init__(self, primary: Any, shadow: Any | None = None):
        self.primary = primary
        self.shadow = shadow

    # ---- delegation passthroughs ----

    @property
    def corpus_size(self) -> int:
        return getattr(self.primary, "corpus_size", 0)

    @property
    def vectorstore(self) -> Any:
        return getattr(self.primary, "vectorstore", None)

    @property
    def shadow_vectorstore(self) -> Any:
        if self.shadow is None:
            return None
        return getattr(self.shadow, "vectorstore", None)

    def invalidate(self) -> None:
        self.primary.invalidate()
        if self.shadow is not None:
            self.shadow.invalidate()

    def rebuild_indexes(self) -> None:
        self.primary.rebuild_indexes()
        if self.shadow is not None:
            self.shadow.rebuild_indexes()

    # ---- retrieval ----

    def retrieve(self, query: str, **kwargs: Any) -> list[Any]:
        return self.retrieve_detailed(query, **kwargs).chunks

    def retrieve_detailed(self, query: str, **kwargs: Any) -> Any:
        result = self.primary.retrieve_detailed(query, **kwargs)
        if self.shadow is not None:
            self._compare_sync(query, kwargs, result)
        return result

    async def aretrieve(self, query: str, **kwargs: Any) -> list[Any]:
        return (await self.aretrieve_detailed(query, **kwargs)).chunks

    async def aretrieve_detailed(self, query: str, **kwargs: Any) -> Any:
        result = await self.primary.aretrieve_detailed(query, **kwargs)
        if self.shadow is not None:
            await asyncio.to_thread(self._compare_sync, query, kwargs, result)
        return result

    # ---- comparison ----

    def _compare_sync(self, query: str, kwargs: dict[str, Any], primary_result: Any) -> None:
        shadow = self.shadow
        if shadow is None:
            return
        try:
            started = time.perf_counter()
            shadow_result = shadow.retrieve_detailed(query, **kwargs)
            elapsed = round((time.perf_counter() - started) * 1000, 2)
        except Exception as exc:  # noqa: BLE001 - shadow must never break the request
            logger.warning("retrieval_shadow_failed", query=query[:120], error=str(exc))
            return

        primary_ids = {getattr(c, "chunk_id", None) for c in getattr(primary_result, "chunks", [])}
        shadow_ids = {getattr(c, "chunk_id", None) for c in getattr(shadow_result, "chunks", [])}
        if primary_ids != shadow_ids:
            logger.warning(
                "retrieval_shadow_diff",
                query=query[:120],
                primary_only=sorted(x for x in primary_ids - shadow_ids if x),
                shadow_only=sorted(x for x in shadow_ids - primary_ids if x),
                elapsed_ms=elapsed,
            )
        else:
            logger.info("retrieval_shadow_match", query=query[:120], count=len(primary_ids), elapsed_ms=elapsed)